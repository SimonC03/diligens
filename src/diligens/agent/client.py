"""The Anthropic client, created only here so API access stays inside agent/."""

import anthropic

from diligens.config import Settings


def make_client(settings: Settings) -> anthropic.Anthropic:
    """Client from the key in .env. The key is passed on, never printed or logged."""
    if settings.anthropic_api_key is None:
        raise ValueError(
            "ANTHROPIC_API_KEY saknas i .env. Lägg till nyckeln eller kör med --no-llm."
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
