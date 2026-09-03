"""The login endpoints, mounted only when the app is configured to own its own OIDC.

The SPA never renders a login form: it gets a 401 from the API and sends the browser to
`/auth/login`, which is the whole of the flow the frontend knows about. Deep links survive because
the router keeps them in the fragment, which the browser does not send upstream and Authentik
therefore cannot lose.
"""

from __future__ import annotations

import logging
from typing import cast

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from x.agentplane.app.oidc import CLIENT_NAME, session_operator, settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class OperatorView(BaseModel):
    """Who the browser is, as the SPA asks on load."""

    model_config = ConfigDict(extra="forbid")

    username: str


def _oauth(request: Request) -> OAuth:
    return cast(OAuth, request.app.state.oauth)


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    client = _oauth(request).create_client(CLIENT_NAME)
    return cast(RedirectResponse, await client.authorize_redirect(request, settings(request).redirect_uri))


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    try:
        client = _oauth(request).create_client(CLIENT_NAME)
        token = await client.authorize_access_token(request)
    except OAuthError as error:
        # The message can carry a value from the query string, so only the code is logged.
        logger.warning("OIDC callback refused: %s", error.error)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"OIDC error: {error.error}") from error

    claims = token.get("userinfo") or {}
    # Pinned because authlib trusts whatever discovery returned; a token from another issuer that
    # happens to validate must not become a session here.
    if claims.get("iss") != settings(request).issuer:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token from an unexpected issuer")
    username = claims.get("preferred_username")
    if not isinstance(username, str) or not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token has no preferred_username")

    request.session["user"] = {"username": username}
    logger.info("operator logged in: %s", username)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.pop("user", None)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me")
async def me(request: Request) -> OperatorView:
    username = session_operator(request)
    if username is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    return OperatorView(username=username)
