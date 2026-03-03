"""Tests for the auth proxy sidecar.

Tests the AutoFetchOAuth2Client (authlib subclass that auto-fetches tokens on
first request) and oauth2_client_factory. Uses respx to intercept token
endpoint requests.
"""

from __future__ import annotations

import pytest_bazel
import respx
from httpx import Response
from starlette.applications import Starlette

from approval_gate.auth_proxy.proxy import AutoFetchOAuth2Client, create_app, oauth2_client_factory

TOKEN_URL = "https://auth.example.com/token"
UPSTREAM_URL = "https://upstream.example.com/mcp"

_TOKEN_RESPONSE = {"token_type": "bearer", "expires_in": 3600}


def _make_client(**kwargs) -> AutoFetchOAuth2Client:
    """Create a test OAuth2 client via the factory."""
    factory = oauth2_client_factory(token_url=TOKEN_URL, client_id="cid", client_secret="csecret", scope="read propose")
    return factory(**kwargs)


@respx.mock
async def test_auto_fetch_gets_token_on_first_request():
    """First request triggers a token fetch transparently."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "fresh-token", **_TOKEN_RESPONSE}))
    respx.get(UPSTREAM_URL).mock(return_value=Response(200))

    client = _make_client()
    resp = await client.get(UPSTREAM_URL)

    assert resp.status_code == 200
    assert client.token["access_token"] == "fresh-token"
    # Upstream request should have the Bearer header
    upstream_req = respx.calls[1].request
    assert upstream_req.headers["Authorization"] == "Bearer fresh-token"


@respx.mock
async def test_token_is_cached_across_requests():
    """Second request reuses cached token without re-fetching."""
    token_route = respx.post(TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "cached-token", **_TOKEN_RESPONSE})
    )
    respx.get(UPSTREAM_URL).mock(return_value=Response(200))

    client = _make_client()
    await client.get(UPSTREAM_URL)
    await client.get(UPSTREAM_URL)

    assert token_route.call_count == 1


@respx.mock
async def test_expired_token_triggers_refresh():
    """Expired token triggers a new fetch on next request."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "new-token", **_TOKEN_RESPONSE}))
    respx.get(UPSTREAM_URL).mock(return_value=Response(200))

    client = _make_client()
    # Pre-set an expired token (expires_at in the distant past)
    client.token = {"access_token": "old-token", "token_type": "bearer", "expires_at": 1}

    await client.get(UPSTREAM_URL)

    assert client.token["access_token"] == "new-token"


@respx.mock
async def test_factory_forwards_transport_kwargs():
    """Factory passes headers/follow_redirects from the transport through."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", **_TOKEN_RESPONSE}))
    respx.get(UPSTREAM_URL).mock(return_value=Response(200))

    client = _make_client(headers={"X-Custom": "val"}, follow_redirects=True)
    await client.get(UPSTREAM_URL)

    upstream_req = respx.calls[1].request
    assert upstream_req.headers["X-Custom"] == "val"


async def test_create_app_returns_starlette():
    """create_app() returns a Starlette ASGI application."""
    factory = oauth2_client_factory(token_url=TOKEN_URL, client_id="cid", client_secret="csecret", scope="read")
    app = create_app(UPSTREAM_URL, factory)
    assert isinstance(app, Starlette)


if __name__ == "__main__":
    pytest_bazel.main()
