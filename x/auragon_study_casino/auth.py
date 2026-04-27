"""OIDC authentication for the Study Casino.

Authorization Code flow (confidential client). The backend exchanges the
code for tokens using `httpx`, then issues an HMAC-SHA256-signed session
cookie. When OIDC is not configured (settings.oidc_client_id is None),
all requests are treated as the "default" user so existing tests and
local dev continue to work unchanged.

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
import urllib.parse
from typing import Annotated

import httpx
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
        auth_ep = disc["authorization_endpoint"]
        state = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
        state_sig = _sign(state, session_secret)
        params = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": callback_url,
                "state": state,
            }
        )
        redirect = RedirectResponse(url=f"{auth_ep}?{params}", status_code=302)
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
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                disc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"token exchange failed: {token_resp.text[:200]}")

        tokens = token_resp.json()
        id_token = tokens.get("id_token", "")
        parts = id_token.split(".")
        if len(parts) < 2:
            raise HTTPException(status_code=502, detail="missing id_token")
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        username = claims.get("preferred_username") or claims.get("sub", "")
        if not username:
            raise HTTPException(status_code=502, detail="no username in id_token")

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
