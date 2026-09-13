"""The login endpoints, mounted only when the app is configured to own its own OIDC.

The SPA never renders a login form: it gets a 401 from the API and sends the browser to
`/auth/login`, which is the whole of the flow the frontend knows about. Deep links survive because
the router keeps them in the fragment, which the browser does not send upstream and Authentik
therefore cannot lose.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import UTC, datetime
from typing import cast

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
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
    except json.JSONDecodeError:
        logger.warning("OIDC token exchange returned a non-JSON response")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Identity provider returned an invalid response; please retry."
        ) from None
    except (OAuthError, JoseError):
        # Even the OAuth error code may be caller-controlled. Never echo provider/query values.
        logger.warning("OIDC callback refused")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC authentication failed") from None

    claims = token.get("userinfo") or {}
    # Pinned because authlib trusts whatever discovery returned; a token from another issuer that
    # happens to validate must not become a session here.
    if claims.get("iss") != settings(request).issuer:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token from an unexpected issuer")
    # This login has one audience. Authlib's ID-token validation alone does not pin it.
    client_id = settings(request).client_id
    if claims.get("aud") not in (client_id, [client_id]) or claims.get("azp", client_id) != client_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token for an unexpected client")
    username = claims.get("preferred_username")
    if not isinstance(username, str) or not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token has no preferred_username")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token has no subject")
    now = time.time()
    id_expiry = claims.get("exp")
    if not isinstance(id_expiry, (float, int)) or not math.isfinite(id_expiry) or id_expiry <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id token has no valid expiry")
    expiry = min(now + settings(request).session_seconds, id_expiry)
    access_token = token.get("access_token")
    token_expiry = token.get("expires_at")
    # Never retain a token without a known lifetime. Login still works, but federation fails closed.
    if (
        request.app.state.operator_actions is not None
        and isinstance(access_token, str)
        and access_token
        and isinstance(token_expiry, (float, int))
        and math.isfinite(token_expiry)
        and token_expiry > now
    ):
        expiry = min(expiry, token_expiry)
    else:
        access_token = None
    request.session.clear()
    request.session["user"] = {
        "issuer": settings(request).issuer,
        "subject": subject,
        "username": username,
        "access_token": access_token,
        "expires_at": expiry,
    }
    request.state.rotate_operator_session = True
    request.state.operator_session_expires_at = datetime.fromtimestamp(expiry, UTC)
    logger.info("operator logged in")
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    if request.headers.get("origin") != settings(request).public_base_url.rstrip("/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "logout requires exact same-origin Origin")
    request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me")
async def me(request: Request) -> OperatorView:
    username = session_operator(request)
    if username is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    return OperatorView(username=username)
