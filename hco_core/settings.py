"""Settings of hco-core, read from `HCO_` environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PSYCOPG_SCHEME = "postgresql+psycopg://"
_PLAIN_SCHEMES = ("postgresql://", "postgres://")


class CoreSettings(BaseSettings):
    """Hold the settings that every hco-core process shares.

    A validation error never shows the input, because a database URL holds a
    password.
    """

    model_config = SettingsConfigDict(
        env_prefix="HCO_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr = SecretStr(f"{PSYCOPG_SCHEME}hco:hco@localhost:5432/hco")
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    sot: str = "yaml"
    sot_path: Path = Path("inventory.yaml")
    connection: str = "mock"
    transcripts_dir: Path = Path("transcripts")
    api_url: str = "http://localhost:8080"
    actor: str | None = None
    validation_key: SecretStr | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def upper_case_log_level(cls, value: object) -> object:
        """Accept a log level in any letter case."""
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def use_the_psycopg_dialect(cls, value: SecretStr) -> SecretStr:
        """Give a plain Postgres URL the psycopg dialect.

        Raises:
            ValueError: If the URL is not a Postgres URL.

        """
        url = value.get_secret_value()
        for scheme in _PLAIN_SCHEMES:
            if url.startswith(scheme):
                return SecretStr(PSYCOPG_SCHEME + url.removeprefix(scheme))
        if not url.startswith(PSYCOPG_SCHEME):
            message = f"use a postgresql:// or {PSYCOPG_SCHEME} URL"
            raise ValueError(message)
        return value
