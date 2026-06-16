"""Backend configuration, read from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTP_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    # Shared secret. When empty, authentication is disabled.
    token: Optional[str] = None
    # Maximum accepted upload / receipt size in megabytes.
    max_upload_mb: int = 25


@lru_cache
def get_settings() -> ServerSettings:
    return ServerSettings()
