"""Authentication for the backend.

Two independent mechanisms live here:

* **Static tokens** (machine-to-machine): the WebSocket printer clients present a
  ``printer_token``; non-interactive HTTP clients (scripts/CI) present an
  ``api_token``. Both are plain shared secrets compared in constant time.
* **OIDC login** (interactive humans): the web UI logs the user in against a
  Gitea instance using the OpenID Connect Authorization Code flow (with PKCE and
  discovery handled by Authlib). On success a signed session cookie identifies
  the user on subsequent requests.

When neither OIDC nor an API token is configured, the HTTP API stays open so the
project keeps working out of the box on a trusted LAN.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from server.config import ServerSettings, get_settings

logger = logging.getLogger("rtp.server")

SESSION_USER_KEY = "user"

_oauth: Optional[OAuth] = None


# --------------------------------------------------------------------------- #
# OIDC client (Authlib)
# --------------------------------------------------------------------------- #
def get_oauth(settings: ServerSettings) -> OAuth:
    """Build (once) and return the Authlib OAuth registry for Gitea."""
    global _oauth
    if _oauth is None:
        oauth = OAuth()
        oauth.register(
            name="gitea",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=(
                settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            ),
            client_kwargs={
                "scope": settings.oidc_scopes,
                # Authorization Code with PKCE (Gitea advertises S256 support).
                "code_challenge_method": "S256",
            },
        )
        _oauth = oauth
    return _oauth


def _redirect_uri(request: Request, settings: ServerSettings) -> str:
    """Absolute callback URL, preferring the configured public URL."""
    if settings.public_url:
        return settings.public_url.rstrip("/") + "/auth/callback"
    return str(request.url_for("auth_callback"))


# --------------------------------------------------------------------------- #
# Allow-list
# --------------------------------------------------------------------------- #
def _claim_str(claims: dict, *names: str) -> str:
    for name in names:
        value = claims.get(name)
        if value:
            return str(value).strip().lower()
    return ""


def user_allowed(claims: dict, settings: ServerSettings) -> bool:
    """Check OIDC claims against the configured user / group allow-lists."""
    allowed_users = settings.allowed_users
    allowed_groups = settings.allowed_groups
    if not allowed_users and not allowed_groups:
        return True

    username = _claim_str(claims, "preferred_username", "name")
    email = _claim_str(claims, "email")
    if allowed_users and (username in allowed_users or email in allowed_users):
        return True

    if allowed_groups:
        groups = {str(g).strip().lower() for g in (claims.get("groups") or [])}
        if groups & allowed_groups:
            return True

    return False


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #
def _store_user(request: Request, claims: dict, settings: ServerSettings) -> dict:
    user = {
        "sub": claims.get("sub"),
        "name": claims.get("name") or claims.get("preferred_username"),
        "username": claims.get("preferred_username"),
        "email": claims.get("email"),
        "picture": claims.get("picture"),
        "exp": int(time.time()) + settings.session_ttl_hours * 3600,
    }
    request.session[SESSION_USER_KEY] = user
    return user


def get_current_user(request: Request) -> Optional[dict]:
    """Return the logged-in user from the session, or ``None`` if not/expired."""
    data = request.session.get(SESSION_USER_KEY)
    if not data:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        request.session.pop(SESSION_USER_KEY, None)
        return None
    return data


# --------------------------------------------------------------------------- #
# HTTP dependency
# --------------------------------------------------------------------------- #
def _bearer_token(
    authorization: Optional[str], x_token: Optional[str], token: Optional[str]
) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return x_token or token


async def require_auth(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> Optional[dict]:
    """Authorize an HTTP request via session cookie *or* static API token.

    Returns the authenticated principal (or ``None`` when auth is disabled) and
    raises ``401`` otherwise.
    """
    settings = get_settings()

    user = get_current_user(request)
    if user is not None:
        return user

    api_token = settings.effective_api_token
    if api_token:
        provided = _bearer_token(authorization, x_token, token)
        if provided and secrets.compare_digest(provided, api_token):
            return {"sub": "api-token", "auth": "token"}

    # No credentials. Only allow through when no auth mechanism is configured.
    if not settings.oidc_active and not api_token:
        return None

    raise HTTPException(status_code=401, detail="Authentication required")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
router = APIRouter()


@router.get("/api/auth/config")
async def auth_config() -> dict:
    """Tell the web UI which auth options are available (no secrets)."""
    settings = get_settings()
    api_token_required = bool(settings.effective_api_token)
    return {
        "oidc_enabled": settings.oidc_active,
        "login_url": "/auth/login",
        "logout_url": "/auth/logout",
        "api_token_required": api_token_required,
        "auth_required": settings.oidc_active or api_token_required,
    }


@router.get("/api/me")
async def me(request: Request) -> dict:
    """Return the current user, or 401 when login is required but absent."""
    settings = get_settings()
    user = get_current_user(request)
    if user is not None:
        return {"user": user}
    if not settings.oidc_active and not settings.effective_api_token:
        return {"user": None, "auth_disabled": True}
    raise HTTPException(status_code=401, detail="Not authenticated")


@router.get("/auth/login")
async def login(request: Request):
    settings = get_settings()
    if not settings.oidc_active:
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")
    oauth = get_oauth(settings)
    return await oauth.gitea.authorize_redirect(request, _redirect_uri(request, settings))


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    settings = get_settings()
    if not settings.oidc_active:
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")
    oauth = get_oauth(settings)

    try:
        token = await oauth.gitea.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("OIDC login failed: %s", exc.error)
        return RedirectResponse(url="/?login=failed", status_code=303)

    claims = token.get("userinfo")
    if not claims:
        claims = await oauth.gitea.userinfo(token=token)

    if not user_allowed(claims, settings):
        logger.warning(
            "Rejected OIDC login for %s (not in allow-list)",
            claims.get("preferred_username") or claims.get("email") or claims.get("sub"),
        )
        request.session.pop(SESSION_USER_KEY, None)
        return RedirectResponse(url="/?login=denied", status_code=303)

    user = _store_user(request, claims, settings)
    logger.info("OIDC login: %s", user.get("username") or user.get("sub"))
    return RedirectResponse(url="/", status_code=303)


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse(url="/", status_code=303)
