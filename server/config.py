"""Backend configuration, read from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Optional, Set

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv_set(value: str) -> Set[str]:
    """Parse a comma-separated string into a set of lower-cased, trimmed items."""
    return {item.strip().lower() for item in value.split(",") if item.strip()}


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTP_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000

    # --- shared secrets (machine-to-machine auth) --------------------------- #
    # Legacy shared secret used for *both* the printer and the HTTP API when the
    # more specific tokens below are not set. Empty disables token auth.
    token: Optional[str] = None
    # Secret the WebSocket printer clients (Raspberry Pi devices) must present.
    printer_token: Optional[str] = None
    # Static bearer token for programmatic HTTP clients (scripts/CI).
    api_token: Optional[str] = None

    # --- OIDC login for web-UI users (Gitea) -------------------------------- #
    oidc_enabled: bool = False
    oidc_issuer: str = "https://git.example.com"
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_scopes: str = "openid profile email groups"
    # Public base URL of *this* backend (e.g. https://printer.example.com).
    # Used to build the OIDC redirect URI; falls back to the request URL.
    public_url: Optional[str] = None
    # Allow-lists (comma separated). When both are empty any authenticated
    # Gitea user may log in; otherwise the user must match at least one entry.
    oidc_allowed_users: str = ""   # usernames or e-mail addresses
    oidc_allowed_groups: str = ""  # Gitea "org" or "org:team" entries

    # --- session cookie ----------------------------------------------------- #
    # Secret used to sign the session cookie. A random one is generated at
    # startup when unset (sessions then reset on restart).
    session_secret: Optional[str] = None
    session_ttl_hours: int = 12

    # Maximum accepted upload / receipt size in megabytes.
    max_upload_mb: int = 25

    # Default ReceiptLine paper width used by the web editor and API defaults.
    receiptline_cpl: int = Field(default=42, ge=24, le=96)

    # --- over-the-air client updates --------------------------------------- #
    # When enabled, a connecting client whose code differs from the code this
    # server ships is sent the new bundle to apply and reload automatically.
    auto_update: bool = True

    # ----------------------------------------------------------------------- #
    # Derived helpers
    # ----------------------------------------------------------------------- #
    @property
    def effective_printer_token(self) -> Optional[str]:
        """Secret expected from WebSocket printer clients."""
        return self.printer_token or self.token

    @property
    def effective_api_token(self) -> Optional[str]:
        """Secret accepted on HTTP endpoints for non-interactive clients."""
        return self.api_token or self.token

    @property
    def oidc_active(self) -> bool:
        """True when OIDC login is enabled *and* sufficiently configured."""
        return bool(
            self.oidc_enabled and self.oidc_client_id and self.oidc_client_secret
        )

    @property
    def allowed_users(self) -> Set[str]:
        return _csv_set(self.oidc_allowed_users)

    @property
    def allowed_groups(self) -> Set[str]:
        return _csv_set(self.oidc_allowed_groups)


@lru_cache
def get_settings() -> ServerSettings:
    return ServerSettings()
