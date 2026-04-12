"""Tests for the grocy auth proxy.

Tests the M2M token flow (username + app_password), header stripping,
and request forwarding. Uses respx to intercept HTTP requests.
"""

from __future__ import annotations

import pytest_bazel
import respx
from httpx import ASGITransport, AsyncClient, Response

from grocy.auth_proxy.proxy import AutoFetchOAuth2Client, create_app, create_client

TOKEN_URL = "https://auth.example.com/application/o/token/"
UPSTREAM_URL = "https://grocy.example.com"

_TOKEN_RESPONSE = {"token_type": "bearer", "expires_in": 3600}


def _make_client() -> AutoFetchOAuth2Client:
    return create_client(
        token_url=TOKEN_URL, client_id="test-client-id", username="grocy-machine", app_password="test-app-password"
    )


def _make_app() -> tuple:
    """Create app and return (app, client_ref) for testing."""
    client_ref: list[AutoFetchOAuth2Client] = []

    def factory() -> AutoFetchOAuth2Client:
        c = _make_client()
        client_ref.append(c)
        return c

    app = create_app(UPSTREAM_URL, factory)
    return app, client_ref


@respx.mock
async def test_auto_fetch_sends_username_and_password():
    """Token fetch sends username and password as form params."""
    token_route = respx.post(TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "jwt-token", **_TOKEN_RESPONSE})
    )
    respx.get(f"{UPSTREAM_URL}/api/system/info").mock(return_value=Response(200, json={"version": "4.6"}))

    client = _make_client()
    await client.get(f"{UPSTREAM_URL}/api/system/info")

    token_req = token_route.calls[0].request
    body = token_req.content.decode()
    assert "username=grocy-machine" in body
    assert "password=test-app-password" in body
    assert "grant_type=client_credentials" in body
    assert "client_id=test-client-id" in body
    # No client_secret: Authentik M2M uses the user's app_password instead.
    assert "client_secret=" not in body


@respx.mock
async def test_token_cached_across_requests():
    """Second request reuses cached token."""
    token_route = respx.post(TOKEN_URL).mock(
        return_value=Response(200, json={"access_token": "cached", **_TOKEN_RESPONSE})
    )
    respx.get(f"{UPSTREAM_URL}/api/stock").mock(return_value=Response(200))

    client = _make_client()
    await client.get(f"{UPSTREAM_URL}/api/stock")
    await client.get(f"{UPSTREAM_URL}/api/stock")

    assert token_route.call_count == 1


@respx.mock
async def test_strips_grocy_api_key_header():
    """GROCY-API-KEY header is stripped before forwarding."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", **_TOKEN_RESPONSE}))
    upstream_route = respx.get(f"{UPSTREAM_URL}/api/stock").mock(return_value=Response(200))

    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.get("/api/stock", headers={"GROCY-API-KEY": "should-be-stripped"})

    upstream_req = upstream_route.calls[0].request
    assert "grocy-api-key" not in {k.lower() for k in upstream_req.headers.keys()}


@respx.mock
async def test_strips_caller_authorization_header():
    """Caller-supplied Authorization header is stripped; proxy's Bearer JWT wins.

    Prevents clients from bypassing the Authentik M2M injection by supplying
    their own credentials to the upstream.
    """
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "proxy-jwt", **_TOKEN_RESPONSE}))
    upstream_route = respx.get(f"{UPSTREAM_URL}/api/stock").mock(return_value=Response(200))

    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.get("/api/stock", headers={"Authorization": "Bearer client-supplied-token"})

    upstream_req = upstream_route.calls[0].request
    assert upstream_req.headers["Authorization"] == "Bearer proxy-jwt"


@respx.mock
async def test_adds_bearer_token():
    """Forwarded request includes Authorization: Bearer header."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "my-jwt", **_TOKEN_RESPONSE}))
    upstream_route = respx.get(f"{UPSTREAM_URL}/api/stock").mock(return_value=Response(200))

    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.get("/api/stock")

    upstream_req = upstream_route.calls[0].request
    assert upstream_req.headers["Authorization"] == "Bearer my-jwt"


@respx.mock
async def test_forwards_method_path_body():
    """Request method, path, query, and body are forwarded."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", **_TOKEN_RESPONSE}))
    upstream_route = respx.post(f"{UPSTREAM_URL}/api/stock/products/1/add?bestbeforedate=2026-12-31").mock(
        return_value=Response(200, json={"ok": True})
    )

    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.post("/api/stock/products/1/add?bestbeforedate=2026-12-31", json={"amount": 5})

    assert resp.status_code == 200
    upstream_req = upstream_route.calls[0].request
    assert upstream_req.method == "POST"
    assert b'"amount"' in upstream_req.content


@respx.mock
async def test_strips_hop_by_hop_response_headers():
    """Hop-by-hop response headers (RFC 7230) are not forwarded to the caller."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", **_TOKEN_RESPONSE}))
    respx.get(f"{UPSTREAM_URL}/api/stock").mock(
        return_value=Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Connection": "keep-alive",
                "Keep-Alive": "timeout=5",
                "X-Custom": "keep-me",
            },
            json={"ok": True},
        )
    )

    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.get("/api/stock")

    header_keys = {k.lower() for k in resp.headers.keys()}
    assert "connection" not in header_keys
    assert "keep-alive" not in header_keys
    assert "x-custom" in header_keys
    assert resp.headers["content-type"] == "application/json"


@respx.mock
async def test_preserves_multiple_set_cookie_headers():
    """Multiple Set-Cookie headers from upstream are preserved, not collapsed."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", **_TOKEN_RESPONSE}))
    # httpx.Response accepts a list of (key, value) tuples to express repeated headers.
    respx.get(f"{UPSTREAM_URL}/api/stock").mock(
        return_value=Response(200, headers=[("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")])
    )

    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.get("/api/stock")

    set_cookies = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    assert len(set_cookies) == 2
    assert "a=1; Path=/" in set_cookies
    assert "b=2; Path=/" in set_cookies


@respx.mock
async def test_upstream_trailing_slash_does_not_double_slash():
    """A trailing slash on UPSTREAM_URL must not produce `//` in forwarded URLs."""
    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "tok", **_TOKEN_RESPONSE}))
    upstream_route = respx.get(f"{UPSTREAM_URL}/api/stock").mock(return_value=Response(200))

    app = create_app(f"{UPSTREAM_URL}/", lambda: _make_client())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.get("/api/stock")

    assert resp.status_code == 200
    assert str(upstream_route.calls[0].request.url) == f"{UPSTREAM_URL}/api/stock"


async def test_health_endpoint():
    """GET /health returns 200."""
    app, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


if __name__ == "__main__":
    pytest_bazel.main()
