"""Authentik OIDC login and signed browser sessions for the pairing page, admitting one owner.

Authentik's application policy already binds the provider to the owner; the check on `sub` here holds if that
binding is ever loosened. The browser keeps only a signed cookie with identity and expiry, never Authentik tokens.
"""

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

from devinfra.claude.session_export.settings import ServeSettings

# SessionMiddleware imports itsdangerous lazily; Gazelle cannot see the runtime dependency.
# gazelle:include_dep @pypi//itsdangerous

logger = logging.getLogger(__name__)

_CLIENT_NAME = "authentik"
_SESSION_COOKIE = "__Host-claude-session-sync"
_INSECURE_SESSION_COOKIE = "claude-session-sync"
_PUBLIC_PATHS = {"/healthz", "/auth/login", "/auth/callback", "/auth/signed-out"}


class LoginSession(BaseModel):
    """The minimum identity data retained in the signed browser session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    subject: str = Field(min_length=1)
    username: str = Field(min_length=1)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


def _session_cookie_name(public_base_url: str) -> str:
    """A host-only Secure cookie in production, a plain name over local HTTP."""
    return _SESSION_COOKIE if public_base_url.startswith("https://") else _INSECURE_SESSION_COOKIE


def _build_oauth(settings: ServeSettings) -> OAuth:
    """A confidential OIDC client with Authorization Code + PKCE."""
    oauth = OAuth()
    oauth.register(
        name=_CLIENT_NAME,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


def _sign_in_failed(message: str) -> HTMLResponse:
    return HTMLResponse(f'<main><p>{message}</p><a href="/auth/login">Try again</a></main>', status_code=401)


def _valid_claims(claims: Any, settings: ServeSettings) -> bool:
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


def _login_router(settings: ServeSettings) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"], include_in_schema=False)

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
            # Provider responses and query parameters are caller-controlled: nothing of them is reflected.
            logger.warning("Authentik sign-in failed")
            return _sign_in_failed("Authentik sign-in failed.")

        claims: Any = token.get("userinfo") or {}
        if not _valid_claims(claims, settings):
            logger.warning("Authentik sign-in returned invalid identity claims")
            return _sign_in_failed("Authentik sign-in failed.")
        if claims["sub"] != settings.oidc_allowed_subject:
            logger.warning("Authentik sign-in by a subject that is not the owner")
            return _sign_in_failed("This page is for its owner only.")

        request.session.clear()
        request.session["user"] = {
            "issuer": settings.oidc_issuer,
            "subject": claims["sub"],
            "username": claims["preferred_username"],
            "expires_at": min(time.time() + settings.oidc_session_seconds, float(claims["exp"])),
        }
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        if request.headers.get("origin") != settings.public_base_url:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Logout requires a same-origin request")
        request.session.clear()
        return RedirectResponse(url="/auth/signed-out", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/signed-out", response_class=HTMLResponse)
    async def signed_out() -> str:
        return '<main><p>You are signed out.</p><a href="/auth/login">Sign in</a></main>'

    return router


def _current_session_is_the_owner(request: Request, settings: ServeSettings) -> bool:
    payload = request.session.get("user")
    if payload is None:
        return False
    try:
        session = LoginSession.model_validate(payload)
    except ValidationError:
        request.session.clear()
        return False
    if (
        session.issuer != settings.oidc_issuer
        or session.subject != settings.oidc_allowed_subject
        or session.expires_at <= time.time()
    ):
        request.session.clear()
        return False
    return True


class RequireOwnerMiddleware(BaseHTTPMiddleware):
    """Protect every route but readiness and the login flow, and refuse cross-origin writes to the API."""

    def __init__(self, app: Any, *, settings: ServeSettings) -> None:
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
        if _current_session_is_the_owner(request, self.settings):
            return await call_next(request)
        if request.url.path.startswith("/api/") or request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)


def install_login(app: FastAPI, settings: ServeSettings) -> None:
    """Install signed sessions outside the owner check and register the login routes."""
    app.state.oauth = _build_oauth(settings)
    # Starlette puts the most recently added middleware outermost, so SessionMiddleware goes on after the
    # owner check and decodes the cookie first.
    app.add_middleware(RequireOwnerMiddleware, settings=settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.oidc_session_secret.get_secret_value(),
        session_cookie=_session_cookie_name(settings.public_base_url),
        max_age=settings.oidc_session_seconds,
        same_site="lax",
        https_only=settings.public_base_url.startswith("https://"),
        path="/",
    )
    app.include_router(_login_router(settings))
