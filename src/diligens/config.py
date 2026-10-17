"""Settings loaded from environment and .env. Secrets are never logged."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-sonnet-5-5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    diligens_model: str = DEFAULT_MODEL

    scb_api_key: SecretStr | None = None

    bolagsverket_client_id: SecretStr | None = None
    bolagsverket_client_secret: SecretStr | None = None
    bolagsverket_env: Literal["test", "prod"] = "test"

    # Where the app keeps analyses and screenings; on this computer only, never in git.
    diligens_workspace: Path = Path("data/private/arbetsyta")

    @field_validator(
        "anthropic_api_key",
        "scb_api_key",
        "bolagsverket_client_id",
        "bolagsverket_client_secret",
        mode="before",
    )
    @classmethod
    def _blank_is_missing(cls, value: object) -> object:
        """`KEY=` in .env means not configured, not an empty secret. Quotes around a value
        are removed: `docker run --env-file` passes KEY="value" with them."""
        if not isinstance(value, str):
            return value
        text = value.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1].strip()
        return text or None

    @property
    def llm_enabled(self) -> bool:
        return self.anthropic_api_key is not None


def get_settings() -> Settings:
    return Settings()
