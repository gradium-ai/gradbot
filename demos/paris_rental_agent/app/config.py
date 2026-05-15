"""Application configuration loaded from env vars."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_SQLITE_URL = f"sqlite+pysqlite:///{_APP_DIR / 'paris_rental.db'}"


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
    secret_key: str = Field(default="change-me")
    tavily_api_key: str = Field(default="")
    google_maps_api_key: str = Field(default="")
    gradium_api_key: str = Field(default="")
    app_env: str = Field(default="development")
    base_url: str = Field(default="http://localhost:8000")
    enable_demo_account: bool = Field(default=False)

    cookie_name: str = Field(default="paris_rental_session")
    cookie_max_age_seconds: int = Field(default=60 * 60 * 24 * 14)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def validate_runtime_security(self) -> None:
        if self.is_production and self.secret_key in {"", "change-me"}:
            raise RuntimeError("SECRET_KEY must be set to a strong unique value in production.")
        if self.is_production and len(self.secret_key.encode("utf-8")) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 bytes in production.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    if not os.environ.get("DATABASE_URL"):
        os.environ.setdefault("DATABASE_URL", _DEFAULT_SQLITE_URL)
    return Settings()
