"""One structured request to Claude with validation and retries. Shared by flagging and
questions so error handling lives in one place.

Transient API trouble (connection, rate limit, overload, server error) raises LlmUnavailable
after the SDK's own retries; callers then fall back to deterministic output. Configuration
problems (bad key, unknown model, rejected request) raise LlmError with a Swedish message
that the CLI shows as is.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import pydantic
from anthropic.types import OutputConfigParam, TextBlockParam

MAX_ATTEMPTS = 3
MAX_TOKENS = 16000

_TRANSIENT = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
)


class LlmError(Exception):
    """The API refuses the configuration; the run should stop with this message."""


class LlmUnavailable(Exception):
    """The API is not reachable right now; callers fall back to deterministic output."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0


def _config_error(exc: anthropic.APIStatusError, model: str) -> LlmError:
    if isinstance(exc, anthropic.AuthenticationError):
        text = "API-nyckeln avvisades. Kontrollera ANTHROPIC_API_KEY i .env eller kör med --no-llm."
    elif isinstance(exc, anthropic.PermissionDeniedError):
        text = f"API-nyckeln saknar behörighet för modellen {model}."
    elif isinstance(exc, anthropic.NotFoundError):
        text = f"Modellen {model} finns inte. Kontrollera DILIGENS_MODEL i .env."
    else:
        text = f"API:t avvisade anropet (status {exc.status_code}). Kör med --no-llm."
    return LlmError(text)


def ask_structured[T: pydantic.BaseModel](
    client: anthropic.Anthropic,
    *,
    model: str,
    system: list[TextBlockParam],
    content: str,
    output_type: type[T],
    check: Callable[[T], list[str]],
    usage: Usage,
    effort: str = "medium",
) -> T | None:
    """Ask for JSON matching `output_type`; return it once `check` finds no problems.

    Problems, cut off answers and schema errors are fed back on the next attempt. Returns None
    after MAX_ATTEMPTS or on a refusal.
    """
    output_config: OutputConfigParam = {
        "effort": effort,  # type: ignore[typeddict-item]
        "format": {"type": "json_schema", "schema": anthropic.transform_schema(output_type)},
    }
    feedback = ""
    for _attempt in range(MAX_ATTEMPTS):
        text = content + (f"\n\nFöregående svar var ogiltigt. {feedback}" if feedback else "")
        usage.calls += 1
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": text}],
                output_config=output_config,
            )
        except _TRANSIENT as exc:
            raise LlmUnavailable(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            raise _config_error(exc, model) from exc
        usage.add(response.usage)

        if response.stop_reason == "refusal":
            return None
        if response.stop_reason == "max_tokens":
            feedback = "Svaret blev för långt och avbröts. Skriv kortare motiveringar."
            continue
        answer = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = output_type.model_validate_json(answer)
        except pydantic.ValidationError as exc:
            feedback = f"Svaret följde inte schemat ({exc.error_count()} fel)."
            continue
        problems = check(parsed)
        if not problems:
            return parsed
        feedback = " ".join(problems)
    return None
