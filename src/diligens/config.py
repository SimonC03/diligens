"""Settings loaded from environment and .env. Secrets are never logged."""

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    diligens_model: str = "claude-sonnet-5-5"

    scb_api_key: SecretStr | None = None

    bolagsverket_client_id: SecretStr | None = None
    bolagsverket_client_secret: SecretStr | None = None
    bolagsverket_env: Literal["test", "prod"] = "test"

    @property
    def llm_enabled(self) -> bool:
        return self.anthropic_api_key is not None


def get_settings() -> Settings:
    return Settings()
