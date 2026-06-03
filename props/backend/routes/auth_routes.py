"""OIDC login routes for the props dashboard (`/auth/*`).

Included by `create_app` only when SSO is configured. The authlib OAuth registry
and the `OIDCSettings` live on `app.state` (set in `create_app`), and the signed
session cookie is provided by Starlette's `SessionMiddleware`.

Flow: `/auth/login` 302s to Authentik; Authentik redirects back to
`/auth/callback`, which exchanges the code, checks the email against the admin
allowlist, and stores `{"email": ...}` in the session. `auth.get_request_identity`
turns that session into a `SessionIdentity` on subsequent requests.
"""

from __future__ import annotations

import logging
from typing import cast

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from props.backend.oidc import AUTHENTIK_CLIENT_NAME, OIDCSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class MeResponse(BaseModel):
    email: str


def _oauth(request: Request) -> OAuth:
    return cast(OAuth, request.app.state.oauth)


def _settings(request: Request) -> OIDCSettings:
    return cast(OIDCSettings, request.app.state.oidc_settings)


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    client = _oauth(request).create_client(AUTHENTIK_CLIENT_NAME)
    return cast(RedirectResponse, await client.authorize_redirect(request, _settings(request).redirect_uri))


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    settings = _settings(request)
    client = _oauth(request).create_client(AUTHENTIK_CLIENT_NAME)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=401, detail=f"OIDC error: {e.error}")

    userinfo = token.get("userinfo")
    if not userinfo or not userinfo.get("email"):
        raise HTTPException(status_code=401, detail="OIDC token missing email claim")

    email = userinfo["email"]
    if not settings.is_admin(email):
        logger.warning(f"Rejected non-admin SSO login: {email=}")
        raise HTTPException(status_code=403, detail=f"{email} is not authorized for props")

    request.session["user"] = {"email": email}
    logger.info(f"SSO login: {email=}")
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.pop("user", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/me")
async def me(request: Request) -> MeResponse:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return MeResponse(email=user["email"])
