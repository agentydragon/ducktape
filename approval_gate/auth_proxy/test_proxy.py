"""Tests for the auth proxy sidecar.

Tests the ClientCredentialsAuth httpx.Auth subclass which handles OAuth2
client_credentials token fetch/cache/refresh. FastMCP's own test suite covers
the proxy forwarding; we test our auth layer.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest_bazel
from starlette.applications import Starlette

from approval_gate.auth_proxy.proxy import ClientCredentialsAuth, create_app

TOKEN_URL = "https://auth.example.com/token"


def _make_token_response(access_token: str = "tok-1", expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access_token, "token_type": "bearer", "expires_in": expires_in},
        request=httpx.Request("POST", TOKEN_URL),
    )


async def test_client_credentials_auth_fetches_token():
    """First request triggers a token fetch and sets Authorization header."""
    auth = ClientCredentialsAuth(token_url=TOKEN_URL, client_id="cid", client_secret="csecret", scope="read propose")
    request = httpx.Request("GET", "https://upstream/mcp")

    with patch("approval_gate.auth_proxy.proxy.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _make_token_response("fresh-token")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        flow = auth.async_auth_flow(request)
        yielded_request = await flow.__anext__()

    assert yielded_request.headers["Authorization"] == "Bearer fresh-token"
    mock_client.post.assert_called_once_with(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "csecret",
            "scope": "read propose",
        },
    )


async def test_client_credentials_auth_caches_token():
    """Second request reuses cached token without re-fetching."""
    auth = ClientCredentialsAuth(token_url=TOKEN_URL, client_id="cid", client_secret="csecret", scope="read")

    with patch("approval_gate.auth_proxy.proxy.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _make_token_response("cached-token")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        # First request — triggers fetch
        req1 = httpx.Request("GET", "https://upstream/mcp")
        flow1 = auth.async_auth_flow(req1)
        await flow1.__anext__()

        # Second request — should reuse token
        req2 = httpx.Request("POST", "https://upstream/mcp")
        flow2 = auth.async_auth_flow(req2)
        yielded = await flow2.__anext__()

    assert yielded.headers["Authorization"] == "Bearer cached-token"
    assert mock_client.post.call_count == 1


async def test_client_credentials_auth_refreshes_expired_token():
    """Expired token triggers a new fetch."""
    auth = ClientCredentialsAuth(token_url=TOKEN_URL, client_id="cid", client_secret="csecret", scope="read")
    # Pre-set an expired token
    auth._access_token = "old-token"
    auth._expires_at = time.monotonic() - 100

    with patch("approval_gate.auth_proxy.proxy.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _make_token_response("new-token")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        req = httpx.Request("GET", "https://upstream/mcp")
        flow = auth.async_auth_flow(req)
        yielded = await flow.__anext__()

    assert yielded.headers["Authorization"] == "Bearer new-token"
    mock_client.post.assert_called_once()


async def test_create_app_returns_starlette():
    """create_app() returns a Starlette ASGI application."""
    auth = ClientCredentialsAuth(
        token_url="https://auth.example.com/token", client_id="unused", client_secret="unused", scope="read"
    )
    app = create_app("https://upstream.example.com/mcp", auth)
    assert isinstance(app, Starlette)


if __name__ == "__main__":
    pytest_bazel.main()
