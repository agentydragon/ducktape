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
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from finance.plaid.db.config import PlaidWebSettings

# SessionMiddleware imports itsdangerous lazily; Gazelle cannot see the runtime dependency.
# gazelle:include_dep @pypi//itsdangerous

logger = logging.getLogger(__name__)

_CLIENT_NAME = "authentik"
_SESSION_COOKIE = "__Host-plaid-link-session"
_INSECURE_SESSION_COOKIE = "plaid-link-session"
_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/auth/signed-out"}


class PlaidLinkSession(BaseModel):
    """The minimum identity data retained in the signed browser session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    subject: str = Field(min_length=1)
    username: str = Field(min_length=1)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


def session_cookie_name(public_base_url: str) -> str:
    """Use a host-only Secure cookie in production and a local-dev name over HTTP."""
    return _SESSION_COOKIE if public_base_url.startswith("https://") else _INSECURE_SESSION_COOKIE


def build_oauth(settings: PlaidWebSettings) -> OAuth:
    """Register a confidential OIDC client with Authorization Code + PKCE."""
    oauth = OAuth()
    oauth.register(
        name=_CLIENT_NAME,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


def create_auth_router(settings: PlaidWebSettings) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    def client(request: Request) -> Any:
        return cast(OAuth, request.app.state.oauth).create_client(_CLIENT_NAME)

    @router.get("/login")
    async def login(request: Request) -> RedirectResponse:
        redirect_uri = f"{settings.public_base_url}/auth/callback"
        return cast(RedirectResponse, await client(request).authorize_redirect(request, redirect_uri))

    @router.get("/callback")
    async def callback(request: Request) -> Response:
        try:
            token = await client(request).authorize_access_token(request)
        except OAuthError, JoseError, json.JSONDecodeError, httpx.HTTPError:
            # Provider responses and query parameters are caller-controlled. Do not reflect
            # arbitrary identity-provider text into the browser.
            logger.warning("Authentik sign-in failed")
            return _sign_in_failed()

        claims: Any = token.get("userinfo") or {}
        if not _valid_claims(claims, settings):
            logger.warning("Authentik sign-in returned invalid identity claims")
            return _sign_in_failed()

        now = time.time()
        expiry = min(now + settings.oidc_session_seconds, float(claims["exp"]))
        request.session.clear()
        # Keep only the identity and expiry. Never store Authentik tokens in the browser cookie.
        request.session["user"] = {
            "issuer": settings.oidc_issuer,
            "subject": claims["sub"],
            "username": claims["preferred_username"],
            "expires_at": expiry,
        }
        logger.info("Authentik session created for Plaid Link")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        if request.headers.get("origin") != settings.public_base_url:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Logout requires a same-origin request")
        request.session.clear()
        return RedirectResponse(url="/auth/signed-out", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/signed-out", response_class=HTMLResponse)
    async def signed_out() -> str:
        return '<main><p>You are signed out of Plaid Link.</p><a href="/auth/login">Sign in</a></main>'

    return router


class RequirePlaidSessionMiddleware(BaseHTTPMiddleware):
    """Protect all browser and API routes while keeping readiness and OIDC callbacks public."""

    def __init__(self, app: Any, *, settings: PlaidWebSettings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        if (
            request.url.path.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != self.settings.public_base_url
        ):
            return JSONResponse({"detail": "Request origin is not allowed"}, status_code=status.HTTP_403_FORBIDDEN)
        if _current_session_is_valid(request, self.settings):
            return await call_next(request)
        if request.url.path.startswith("/api/") or request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)


def install_oidc_auth(app: FastAPI, settings: PlaidWebSettings) -> None:
    """Install signed sessions outside the session-check middleware and register OIDC routes."""
    app.state.oauth = build_oauth(settings)
    # Starlette puts the most recently added middleware on the outside. SessionMiddleware
    # therefore needs to be added after the auth checker so it decodes the cookie first.
    app.add_middleware(RequirePlaidSessionMiddleware, settings=settings)
    secure_cookie = settings.public_base_url.startswith("https://")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.oidc_session_secret.get_secret_value(),
        session_cookie=session_cookie_name(settings.public_base_url),
        max_age=settings.oidc_session_seconds,
        same_site="lax",
        https_only=secure_cookie,
        path="/",
    )
    app.include_router(create_auth_router(settings))


def _current_session_is_valid(request: Request, settings: PlaidWebSettings) -> bool:
    payload = request.session.get("user")
    if payload is None:
        return False
    try:
        session = PlaidLinkSession.model_validate(payload)
    except ValidationError:
        request.session.clear()
        return False
    if session.issuer != settings.oidc_issuer or session.expires_at <= time.time():
        request.session.clear()
        return False
    return True


def _sign_in_failed() -> HTMLResponse:
    return HTMLResponse(
        '<main><p>Authentik sign-in failed.</p><a href="/auth/login">Try again</a></main>', status_code=401
    )


def _valid_claims(claims: Any, settings: PlaidWebSettings) -> bool:
    if not isinstance(claims, dict) or claims.get("iss") != settings.oidc_issuer:
        return False

    aud = claims.get("aud")
    if isinstance(aud, str):
        audiences = {aud}
    elif isinstance(aud, list) and all(isinstance(value, str) for value in aud):
        audiences = set(aud)
    else:
        return False

    azp = claims.get("azp")
    if settings.oidc_client_id not in audiences or (azp is not None and azp != settings.oidc_client_id):
        return False
    if len(audiences) > 1 and azp != settings.oidc_client_id:
        return False

    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry):
        return False
    if expiry <= time.time():
        return False

    return all(isinstance(claims.get(name), str) and claims[name] for name in ("sub", "preferred_username"))
