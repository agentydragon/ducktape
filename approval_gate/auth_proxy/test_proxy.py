"""Tests for the auth proxy sidecar.

Tests the ClientCredentialsAuth httpx.Auth subclass which delegates OAuth2
client_credentials token management to authlib's AsyncOAuth2Client. Uses
respx to intercept token endpoint requests.
"""

from __future__ import annotations

import httpx
import pytest_bazel
import respx
from httpx import Response
from starlette.applications import Starlette

from approval_gate.auth_proxy.proxy import ClientCredentialsAuth, create_app

TOKEN_URL = "https://auth.example.com/token"

_TOKEN_RESPONSE = {"token_type": "bearer", "expires_in": 3600}


def _make_auth() -> ClientCredentialsAuth:
    return ClientCredentialsAuth(token_url=TOKEN_URL, client_id="cid", client_secret="csecret", scope="read propose")


@respx.mock
async def test_client_credentials_auth_fetches_token():
    """First request triggers a token fetch and sets Authorization header."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "fresh-token", **_TOKEN_RESPONSE}))
    auth = _make_auth()
    request = httpx.Request("GET", "https://upstream/mcp")

    flow = auth.async_auth_flow(request)
    yielded_request = await flow.__anext__()

    assert yielded_request.headers["Authorization"] == "Bearer fresh-token"


@respx.mock
async def test_client_credentials_auth_caches_token():
    """Second request reuses cached token without re-fetching."""
    route = respx.post(TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "cached-token", **_TOKEN_RESPONSE})
    )
    auth = _make_auth()

    # First request — triggers fetch
    req1 = httpx.Request("GET", "https://upstream/mcp")
    flow1 = auth.async_auth_flow(req1)
    await flow1.__anext__()

    # Second request — should reuse token
    req2 = httpx.Request("POST", "https://upstream/mcp")
    flow2 = auth.async_auth_flow(req2)
    yielded = await flow2.__anext__()

    assert yielded.headers["Authorization"] == "Bearer cached-token"
    assert route.call_count == 1


@respx.mock
async def test_client_credentials_auth_refreshes_expired_token():
    """Expired token triggers a new fetch."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "new-token", **_TOKEN_RESPONSE}))
    auth = _make_auth()
    # Pre-set an expired token (expires_at in the distant past)
    auth._oauth.token = {"access_token": "old-token", "token_type": "bearer", "expires_at": 1}

    req = httpx.Request("GET", "https://upstream/mcp")
    flow = auth.async_auth_flow(req)
    yielded = await flow.__anext__()

    assert yielded.headers["Authorization"] == "Bearer new-token"


async def test_create_app_returns_starlette():
    """create_app() returns a Starlette ASGI application."""
    auth = _make_auth()
    app = create_app("https://upstream.example.com/mcp", auth)
    assert isinstance(app, Starlette)


if __name__ == "__main__":
    pytest_bazel.main()
