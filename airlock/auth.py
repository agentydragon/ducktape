"""Authentik login and the browser's short-lived Airlock session.

Shares only the claims check and client registration with `util/oidc_login.py`; its docstring says why the routes and
guard stay here.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Annotated, Any, cast

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette import status

from airlock.settings import Settings
from util.oidc_login import CLIENT_NAME, valid_claims

logger = logging.getLogger(__name__)

SECURE_SESSION_COOKIE = "__Host-airlock_session"
INSECURE_SESSION_COOKIE = "airlock_session"


class OperatorSession(BaseModel):
    """The minimum identity data needed to authorize browser requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    subject: str
    username: str
    expires_at: float


def session_cookie_name(settings: Settings) -> str:
    """Use the host-only secure cookie name on HTTPS and a local-dev name on HTTP."""
    return SECURE_SESSION_COOKIE if settings.public_base_url.startswith("https://") else INSECURE_SESSION_COOKIE


def current_operator_session(request: Request, settings: Settings) -> OperatorSession | None:
    """Return a valid session identity, clearing malformed or expired sessions."""
    if "session" not in request.scope:
        return None
    payload = request.session.get("user")
    if payload is None:
        return None
    try:
        operator = OperatorSession.model_validate(payload)
    except ValidationError:
        request.session.clear()
        return None
    if (
        operator.issuer != settings.oidc_issuer
        or not operator.subject
        or not operator.username
        or not math.isfinite(operator.expires_at)
        or operator.expires_at <= time.time()
    ):
        request.session.clear()
        return None
    return operator


async def require_operator_session(request: Request) -> OperatorSession:
    settings = cast(Settings, request.app.state.settings)
    operator = current_operator_session(request, settings)
    if operator is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return operator


OperatorSessionDependency = Annotated[OperatorSession, Depends(require_operator_session)]


def create_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    def client(request: Request) -> Any:
        return cast(OAuth, request.app.state.oauth).create_client(CLIENT_NAME)

    @router.get("/login")
    async def login(request: Request) -> RedirectResponse:
        return cast(RedirectResponse, await client(request).authorize_redirect(request, _redirect_uri(settings)))

    @router.get("/callback")
    async def callback(request: Request) -> RedirectResponse:
        try:
            token = await client(request).authorize_access_token(request)
        except OAuthError, JoseError, json.JSONDecodeError, httpx.HTTPError:
            # The provider response and its query parameters are caller-controlled. Keep the
            # browser message useful without reflecting arbitrary identity-provider text.
            logger.warning("OIDC sign-in failed")
            return _sign_in_failed()

        claims: Any = token.get("userinfo") or {}
        if not valid_claims(claims, issuer=settings.oidc_issuer, client_id=settings.oidc_client_id):
            logger.warning("OIDC sign-in returned invalid identity claims")
            return _sign_in_failed()

        now = time.time()
        expiry = min(now + settings.session_seconds, float(claims["exp"]))
        request.session.clear()
        # Never retain the Authentik access token or ID token in the browser session.
        request.session["user"] = {
            "issuer": settings.oidc_issuer,
            "subject": claims["sub"],
            "username": claims["preferred_username"],
            "expires_at": expiry,
        }
        logger.info("OIDC operator session created")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        if request.headers.get("origin") != settings.public_base_url:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Logout requires a same-origin request")
        request.session.clear()
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return router


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url}/auth/callback"


def _sign_in_failed() -> RedirectResponse:
    return RedirectResponse(url="/?auth=failed", status_code=status.HTTP_303_SEE_OTHER)
