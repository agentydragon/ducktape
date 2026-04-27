"""OIDC authentication for the Study Casino.

Authorization Code flow (confidential client). The backend exchanges the
code for tokens using authlib, then calls the userinfo endpoint to get
the username, and issues an HMAC-SHA256-signed session cookie. When OIDC
is not configured, all requests are treated as the "default" user so
existing tests and local dev continue to work unchanged.

Cookie format: base64url(JSON payload) + "." + hex(HMAC-SHA256 signature)
Payload JSON: {"sub": "<username>", "exp": <unix-ts>}
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import time
from typing import Annotated

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

_discovery_cache: dict[str, dict] = {}


async def _get_discovery(issuer: str) -> dict:
    if issuer not in _discovery_cache:
        async with httpx.AsyncClient() as client:
            resp = await client.get(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10)
            resp.raise_for_status()
            _discovery_cache[issuer] = resp.json()
    return _discovery_cache[issuer]


def _sign(payload: str, secret: bytes) -> str:
    return hmac_mod.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(username: str, secret: bytes, ttl_seconds: int = 30 * 24 * 3600) -> str:
    exp = int(time.time()) + ttl_seconds
    raw = json.dumps({"sub": username, "exp": exp}, separators=(",", ":"))
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = _sign(payload, secret)
    return f"{payload}.{sig}"


def decode_session_token(token: str, secret: bytes) -> str | None:
    """Return username or None if token is invalid/expired."""
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac_mod.compare_digest(_sign(payload, secret), sig):
        return None
    padded = payload + "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    sub = data.get("sub", "")
    return sub if sub else None


def create_oidc_router(
    issuer: str, client_id: str, client_secret: str, session_secret: bytes, public_url: str
) -> APIRouter:
    router = APIRouter(prefix="/auth")
    callback_url = public_url.rstrip("/") + "/auth/callback"

    @router.get("/login")
    async def login() -> RedirectResponse:
        disc = await _get_discovery(issuer)
        state = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
        state_sig = _sign(state, session_secret)
        async with AsyncOAuth2Client(client_id=client_id, redirect_uri=callback_url) as oauth:
            url, _ = oauth.create_authorization_url(disc["authorization_endpoint"], state=state, scope="openid profile")
        redirect = RedirectResponse(url=url, status_code=302)
        redirect.set_cookie(
            "casino_state", f"{state}.{state_sig}", httponly=True, secure=True, samesite="lax", max_age=600
        )
        return redirect

    @router.get("/callback")
    async def callback(code: str, state: str, casino_state: Annotated[str | None, Cookie()] = None) -> RedirectResponse:
        if casino_state is None:
            raise HTTPException(status_code=400, detail="missing state cookie")
        try:
            stored_state, stored_sig = casino_state.rsplit(".", 1)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid state cookie")
        if not hmac_mod.compare_digest(stored_sig, _sign(stored_state, session_secret)):
            raise HTTPException(status_code=400, detail="state signature invalid")
        if state != stored_state:
            raise HTTPException(status_code=400, detail="state mismatch")

        disc = await _get_discovery(issuer)
        try:
            async with AsyncOAuth2Client(
                client_id=client_id, client_secret=client_secret, redirect_uri=callback_url
            ) as oauth:
                await oauth.fetch_token(disc["token_endpoint"], code=code, grant_type="authorization_code")
                userinfo_resp = await oauth.get(disc["userinfo_endpoint"])
                userinfo_resp.raise_for_status()
                userinfo = userinfo_resp.json()
        except Exception as exc:
            logger.warning("OIDC token exchange or userinfo failed: %s", exc)
            raise HTTPException(status_code=502, detail="authentication failed") from exc

        username = userinfo.get("preferred_username") or userinfo.get("sub", "")
        if not username:
            raise HTTPException(status_code=502, detail="no username in userinfo")

        logger.info("OIDC login: user=%s", username)
        session_token = make_session_token(username, session_secret)
        redirect = RedirectResponse(url="/", status_code=303)
        redirect.delete_cookie("casino_state")
        redirect.set_cookie(
            "casino_session", session_token, httponly=True, secure=True, samesite="lax", max_age=30 * 24 * 3600
        )
        return redirect

    @router.get("/logout")
    async def logout() -> RedirectResponse:
        redirect = RedirectResponse(url="/", status_code=303)
        redirect.delete_cookie("casino_session")
        return redirect

    return router


def make_current_user_dep(session_secret: bytes | None) -> object:
    """Return a FastAPI dependency that resolves the current username.

    When session_secret is None (OIDC not configured), always returns
    "default" so tests and local dev work without authentication.
    """
    if session_secret is None:

        def no_auth_user() -> str:
            return "default"

        return no_auth_user

    def get_user(casino_session: Annotated[str | None, Cookie()] = None) -> str:
        if casino_session is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        username = decode_session_token(casino_session, session_secret)
        if username is None:
            raise HTTPException(status_code=401, detail="session invalid or expired")
        return username

    return get_user
