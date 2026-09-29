"""Authentik OIDC login and signed browser sessions for the Plaid Link UI."""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, cast

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc.errors import JoseError
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from finance.plaid.db.config import PlaidWebSettings

logger = logging.getLogger(__name__)

_CLIENT_NAME = "authentik"
_SESSION_COOKIE = "__Host-plaid-link-session"
_INSECURE_SESSION_COOKIE = "plaid-link-session"
_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/auth/signed-out"}


class PlaidOidcSettings(BaseSettings):
    """Secrets and issuer for the server-side Authentik client.

    The daily sync process uses `PlaidWebSettings` only and never receives these values.
    """

    model_config = SettingsConfigDict(env_prefix="PLAID_MCP_OIDC_")

    issuer: str
    client_id: str
    client_secret: SecretStr
    session_secret: SecretStr
    session_seconds: int = Field(default=28_800, gt=0)


def session_cookie_name(public_base_url: str) -> str:
    """Use a host-only Secure cookie in production and a local-dev name over HTTP."""
    return _SESSION_COOKIE if public_base_url.startswith("https://") else _INSECURE_SESSION_COOKIE


def build_oauth(settings: PlaidOidcSettings) -> OAuth:
    """Register a confidential OIDC client with Authorization Code + PKCE."""
    oauth = OAuth()
    oauth.register(
        name=_CLIENT_NAME,
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        server_metadata_url=f"{settings.issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


def create_auth_router(web_settings: PlaidWebSettings, oidc_settings: PlaidOidcSettings) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    def client(request: Request) -> Any:
        return cast(OAuth, request.app.state.oauth).create_client(_CLIENT_NAME)

    @router.get("/login")
    async def login(request: Request) -> RedirectResponse:
        redirect_uri = f"{web_settings.public_base_url}/auth/callback"
        return cast(RedirectResponse, await client(request).authorize_redirect(request, redirect_uri))

    @router.get("/callback")
    async def callback(request: Request) -> Response:
        try:
            token = await client(request).authorize_access_token(request)
        except (OAuthError, JoseError, json.JSONDecodeError, httpx.HTTPError):
            # Provider responses and query parameters are caller-controlled. Do not reflect
            # arbitrary identity-provider text into the browser.
            logger.warning("Authentik sign-in failed")
            return _sign_in_failed()

        claims: Any = token.get("userinfo") or {}
        if not _valid_claims(claims, oidc_settings):
            logger.warning("Authentik sign-in returned invalid identity claims")
            return _sign_in_failed()

        now = time.time()
        expiry = min(now + oidc_settings.session_seconds, float(claims["exp"]))
        request.session.clear()
        # Keep only the identity and expiry. Never store Authentik tokens in the browser cookie.
        request.session["user"] = {
            "issuer": oidc_settings.issuer,
            "subject": claims["sub"],
            "username": claims["preferred_username"],
            "expires_at": expiry,
        }
        logger.info("Authentik session created for Plaid Link")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        if request.headers.get("origin") != web_settings.public_base_url:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Logout requires a same-origin request")
        request.session.clear()
        return RedirectResponse(url="/auth/signed-out", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/signed-out", response_class=HTMLResponse)
    async def signed_out() -> str:
        return '<main><p>You are signed out of Plaid Link.</p><a href="/auth/login">Sign in</a></main>'

    return router


class RequirePlaidSessionMiddleware(BaseHTTPMiddleware):
    """Protect all browser and API routes while keeping readiness and OIDC callbacks public."""

    def __init__(self, app: Any, *, public_base_url: str, oidc_settings: PlaidOidcSettings) -> None:
        super().__init__(app)
        self.public_base_url = public_base_url
        self.oidc_settings = oidc_settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        if (
            request.url.path.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != self.public_base_url
        ):
            return JSONResponse({"detail": "Request origin is not allowed"}, status_code=status.HTTP_403_FORBIDDEN)
        if _current_session_is_valid(request, self.oidc_settings):
            return await call_next(request)
        if request.url.path.startswith("/api/") or request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)


def install_oidc_auth(app: FastAPI, web_settings: PlaidWebSettings, oidc_settings: PlaidOidcSettings) -> None:
    """Install signed sessions outside the session-check middleware and register OIDC routes."""
    app.state.oauth = build_oauth(oidc_settings)
    # Starlette puts the most recently added middleware on the outside. SessionMiddleware
    # therefore needs to be added after the auth checker so it decodes the cookie first.
    app.add_middleware(
        RequirePlaidSessionMiddleware,
        public_base_url=web_settings.public_base_url,
        oidc_settings=oidc_settings,
    )
    secure_cookie = web_settings.public_base_url.startswith("https://")
    app.add_middleware(
        SessionMiddleware,
        secret_key=oidc_settings.session_secret.get_secret_value(),
        session_cookie=session_cookie_name(web_settings.public_base_url),
        max_age=oidc_settings.session_seconds,
        same_site="lax",
        https_only=secure_cookie,
        path="/",
    )
    app.include_router(create_auth_router(web_settings, oidc_settings))


def _current_session_is_valid(request: Request, settings: PlaidOidcSettings) -> bool:
    session = request.session.get("user")
    if not isinstance(session, dict):
        request.session.clear()
        return False
    issuer = session.get("issuer")
    subject = session.get("subject")
    username = session.get("username")
    expires_at = session.get("expires_at")
    if (
        issuer != settings.issuer
        or not isinstance(subject, str)
        or not subject
        or not isinstance(username, str)
        or not username
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (float, int))
        or not math.isfinite(expires_at)
        or expires_at <= time.time()
    ):
        request.session.clear()
        return False
    return True


def _sign_in_failed() -> HTMLResponse:
    return HTMLResponse(
        '<main><p>Authentik sign-in failed.</p><a href="/auth/login">Try again</a></main>', status_code=401
    )


def _valid_claims(claims: Any, settings: PlaidOidcSettings) -> bool:
    if not isinstance(claims, dict) or claims.get("iss") != settings.issuer:
        return False

    aud = claims.get("aud")
    if isinstance(aud, str):
        audiences = {aud}
    elif isinstance(aud, list) and all(isinstance(value, str) for value in aud):
        audiences = set(aud)
    else:
        return False

    azp = claims.get("azp")
    if settings.client_id not in audiences or (azp is not None and azp != settings.client_id):
        return False
    if len(audiences) > 1 and azp != settings.client_id:
        return False

    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry):
        return False
    if expiry <= time.time():
        return False

    return all(isinstance(claims.get(name), str) and claims[name] for name in ("sub", "preferred_username"))
