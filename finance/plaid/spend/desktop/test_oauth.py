"""Tests for the local OAuth callback's HTTP contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import httpx
import httpx2
import pytest
import pytest_bazel
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient, TestServer
from authlib.integrations.httpx_client import AsyncOAuth2Client as AuthlibAsyncOAuth2Client

from finance.plaid.spend.desktop import oauth as oauth_module
from finance.plaid.spend.desktop.credentials import SecretServiceTokenStore
from finance.plaid.spend.desktop.oauth import (
    REDIRECT_URI,
    AuthenticationRequiredError,
    Authenticator,
    CallbackResult,
    OAuthConfig,
    _callback_app,
)

STATE = "test-oauth-state"
CONTENT_SECURITY_POLICY = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
ISSUER = "https://auth.example.invalid/application/o/plaid-spend-desktop/"


def _test_client(callback_future: asyncio.Future[CallbackResult]) -> TestClient:
    return TestClient(TestServer(_callback_app(STATE, callback_future)))


def _assert_response_headers(response: ClientResponse) -> None:
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["Connection"] == "close"


class _MemoryTokenStore(SecretServiceTokenStore):
    def __init__(self, refresh_token: str | None) -> None:
        self.refresh_token = refresh_token
        self.saved: list[str] = []
        self.clear_count = 0

    async def load_refresh_token(self) -> str | None:
        return self.refresh_token

    async def save_refresh_token(self, token: str) -> None:
        self.refresh_token = token
        self.saved.append(token)

    async def clear(self) -> None:
        self.refresh_token = None
        self.clear_count += 1


class _ExitedProcess:
    returncode = 0

    async def wait(self) -> int:
        return self.returncode


def _discovery_client() -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{ISSUER}.well-known/openid-configuration"
        return httpx.Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": "https://auth.example.invalid/authorize",
                "token_endpoint": "https://auth.example.invalid/token",
            },
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


def _install_oauth_transport(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx2.Request], httpx2.Response]
) -> None:
    class OAuthClient(AuthlibAsyncOAuth2Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(oauth_module, "AsyncOAuth2Client", OAuthClient)


@pytest.mark.asyncio
async def test_callback_http_rejects_wrong_method_and_path() -> None:
    callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()
    async with _test_client(callback_future) as client:
        response = await client.post(f"/callback?state={STATE}")
        assert response.status == 400
        assert "Invalid callback request." in await response.text()
        _assert_response_headers(response)

        response = await client.get(f"/unknown?state={STATE}")
        assert response.status == 404
        assert "Unknown callback path." in await response.text()
        _assert_response_headers(response)

    assert not callback_future.done()


@pytest.mark.asyncio
async def test_callback_http_rejects_wrong_state_and_fails_login() -> None:
    callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()
    async with _test_client(callback_future) as client:
        response = await client.get("/callback?state=wrong&code=authorization-code")
        assert response.status == 400
        assert "Login state did not match." in await response.text()
        _assert_response_headers(response)

    with pytest.raises(RuntimeError, match="invalid login state"):
        await callback_future


@pytest.mark.asyncio
async def test_callback_http_returns_provider_error_safely() -> None:
    callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()
    description = "bad\x1b_request <script> & " + "x" * 300
    expected_description = ("bad_request <script> & " + "x" * 300)[:240]
    escaped_description = "bad_request &lt;script&gt; &amp; " + "x" * (240 - len("bad_request <script> & "))
    async with _test_client(callback_future) as client:
        response = await client.get(
            "/callback", params={"state": STATE, "error": "invalid_request", "error_description": description}
        )
        assert response.status == 200
        body = await response.text()
        assert "authorization_code grant" in body
        assert escaped_description in body
        assert "<script>" not in body
        _assert_response_headers(response)

    assert await callback_future == CallbackResult(error="invalid_request", error_description=expected_description)


@pytest.mark.asyncio
async def test_callback_http_requires_code_after_valid_state() -> None:
    callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()
    async with _test_client(callback_future) as client:
        response = await client.get(f"/callback?state={STATE}")
        assert response.status == 400
        assert "No authorization code was returned." in await response.text()
        _assert_response_headers(response)

    with pytest.raises(RuntimeError, match="did not return an authorization code"):
        await callback_future


@pytest.mark.asyncio
async def test_callback_http_completes_login_with_code() -> None:
    callback_future: asyncio.Future[CallbackResult] = asyncio.get_running_loop().create_future()
    async with _test_client(callback_future) as client:
        response = await client.get(f"/callback?state={STATE}&code=authorization%2Bcode")
        assert response.status == 200
        assert "Plaid Spend is connected." in await response.text()
        _assert_response_headers(response)

    assert await callback_future == CallbackResult(code="authorization+code")


async def test_login_uses_authlib_for_public_pkce_and_token_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    token_forms: list[dict[str, list[str]]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://auth.example.invalid/token"
        assert request.method == "POST"
        assert "authorization" not in request.headers
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        form = parse_qs(request.content.decode())
        token_forms.append(form)
        return httpx2.Response(
            200,
            json={
                "access_token": "login-access-token",
                "refresh_token": "login-refresh-token",
                "expires_in": "1800",
                "token_type": "Bearer",
            },
            request=request,
        )

    _install_oauth_transport(monkeypatch, respond)
    opened_urls: list[str] = []

    async def open_browser(*args: str, **_kwargs: object) -> _ExitedProcess:
        assert args[0] == "xdg-open"
        authorization_url = args[1]
        opened_urls.append(authorization_url)
        authorization_params = parse_qs(urlsplit(authorization_url).query)
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                REDIRECT_URI, params={"state": authorization_params["state"][0], "code": "authorization-code"}
            )
        assert response.status == 200
        return _ExitedProcess()

    monkeypatch.setattr(oauth_module.asyncio, "create_subprocess_exec", open_browser)
    store = _MemoryTokenStore(None)
    http = _discovery_client()
    try:
        authenticator = Authenticator(OAuthConfig(issuer=ISSUER, client_id="plaid-spend-desktop"), http, store)
        await authenticator.login()
    finally:
        await http.aclose()

    assert len(opened_urls) == 1
    authorization_params = parse_qs(urlsplit(opened_urls[0]).query)
    assert authorization_params["client_id"] == ["plaid-spend-desktop"]
    assert authorization_params["response_type"] == ["code"]
    assert authorization_params["redirect_uri"] == [REDIRECT_URI]
    assert authorization_params["scope"] == ["openid offline_access"]
    assert authorization_params["code_challenge_method"] == ["S256"]
    assert len(token_forms) == 1
    token_form = token_forms[0]
    assert token_form["grant_type"] == ["authorization_code"]
    assert token_form["client_id"] == ["plaid-spend-desktop"]
    assert token_form["code"] == ["authorization-code"]
    assert token_form["redirect_uri"] == [REDIRECT_URI]
    assert "client_secret" not in token_form
    verifier = token_form["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert authorization_params["code_challenge"] == [challenge.rstrip(b"=").decode("ascii")]
    assert store.refresh_token == "login-refresh-token"
    assert store.saved == ["login-refresh-token"]
    assert authenticator.access_token == "login-access-token"
    assert authenticator.access_token_expires_at > 0


@pytest.mark.parametrize(
    ("response_token", "expected_refresh_token", "expected_saves"),
    [
        pytest.param(
            {"access_token": "refreshed-access-token", "refresh_token": "rotated-refresh-token", "expires_in": 1200},
            "rotated-refresh-token",
            ["rotated-refresh-token"],
            id="rotated-refresh-token-is-persisted",
        ),
        pytest.param(
            {"access_token": "refreshed-access-token", "expires_in": "1200"},
            "stored-refresh-token",
            [],
            id="unchanged-refresh-token-is-not-rewritten",
        ),
    ],
)
async def test_refresh_uses_public_client_and_preserves_token_rotation(
    monkeypatch: pytest.MonkeyPatch,
    response_token: dict[str, object],
    expected_refresh_token: str,
    expected_saves: list[str],
) -> None:
    token_forms: list[dict[str, list[str]]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://auth.example.invalid/token"
        assert request.method == "POST"
        assert "authorization" not in request.headers
        form = parse_qs(request.content.decode())
        token_forms.append(form)
        return httpx2.Response(200, json=response_token, request=request)

    _install_oauth_transport(monkeypatch, respond)
    store = _MemoryTokenStore("stored-refresh-token")
    http = _discovery_client()
    try:
        authenticator = Authenticator(OAuthConfig(issuer=ISSUER, client_id="plaid-spend-desktop"), http, store)
        expires_before = time.monotonic()
        access_token = await authenticator.access_token_for_api(force_refresh=True)
        expires_after = time.monotonic()
    finally:
        await http.aclose()

    assert access_token == "refreshed-access-token"
    assert len(token_forms) == 1
    assert token_forms[0]["grant_type"] == ["refresh_token"]
    assert token_forms[0]["client_id"] == ["plaid-spend-desktop"]
    assert token_forms[0]["refresh_token"] == ["stored-refresh-token"]
    assert "scope" not in token_forms[0]
    assert store.refresh_token == expected_refresh_token
    assert store.saved == expected_saves
    assert expires_before + 1199 < authenticator.access_token_expires_at < expires_after + 1200


@pytest.mark.parametrize("status", [400, 401])
async def test_invalid_refresh_status_clears_the_persisted_credential(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": "invalid_grant"}, request=request)

    _install_oauth_transport(monkeypatch, respond)
    store = _MemoryTokenStore("expired-refresh-token")
    http = _discovery_client()
    try:
        authenticator = Authenticator(OAuthConfig(issuer=ISSUER, client_id="plaid-spend-desktop"), http, store)
        authenticator.access_token = "expired-access-token"
        authenticator.access_token_expires_at = time.monotonic() - 1
        with pytest.raises(AuthenticationRequiredError, match="sign-in expired"):
            await authenticator.access_token_for_api()
    finally:
        await http.aclose()

    assert store.refresh_token is None
    assert store.clear_count == 1
    assert authenticator.access_token is None
    assert authenticator.access_token_expires_at == 0.0


async def test_authlib_error_description_does_not_escape_the_token_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    error_description = "response detail mentioning stored-refresh-token"

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"error": "invalid_grant", "error_description": error_description}, request=request
        )

    _install_oauth_transport(monkeypatch, respond)
    store = _MemoryTokenStore("stored-refresh-token")
    http = _discovery_client()
    try:
        authenticator = Authenticator(OAuthConfig(issuer=ISSUER, client_id="plaid-spend-desktop"), http, store)
        with pytest.raises(RuntimeError, match="Authentik rejected the token request") as error:
            await authenticator.access_token_for_api(force_refresh=True)
    finally:
        await http.aclose()

    assert error_description not in str(error.value)
    assert error.value.__cause__ is None
    assert store.refresh_token == "stored-refresh-token"
    assert store.clear_count == 0


if __name__ == "__main__":
    pytest_bazel.main()
