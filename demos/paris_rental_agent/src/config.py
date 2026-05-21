"""Application configuration loaded from env vars."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_SQLITE_URL = f"sqlite+pysqlite:///{_APP_DIR / 'paris_rental.db'}"
_SECRET_PLACEHOLDERS = {
    "",
    "change-me",
    "change-me-generate-a-random-secret",
    "generate-with-python-secrets-token-urlsafe-32",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(
        default=_DEFAULT_SQLITE_URL,
        description="SQLAlchemy database URL. Falls back to local SQLite if not provided.",
    )
    secret_key: str = Field(default="")
    tavily_api_key: str = Field(default="")
    google_maps_api_key: str = Field(default="")
    gradium_api_key: str = Field(default="")
    app_env: str = Field(default="development")
    base_url: str = Field(default="http://localhost:8000")

    cookie_name: str = Field(default="paris_rental_session")
    cookie_max_age_seconds: int = Field(default=60 * 60 * 24 * 14)

    @property
    def effective_secret_key(self) -> str:
        if self.secret_key in _SECRET_PLACEHOLDERS:
            raise RuntimeError(
                "SECRET_KEY is not configured. Set a strong private value in .env."
            )
        return self.secret_key

    def validate_runtime_security(self) -> None:
        if self.secret_key in _SECRET_PLACEHOLDERS:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong unique value in .env."
            )
        if len(self.secret_key.encode("utf-8")) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 bytes.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    if not os.environ.get("DATABASE_URL"):
        os.environ.setdefault("DATABASE_URL", _DEFAULT_SQLITE_URL)
    return Settings()
