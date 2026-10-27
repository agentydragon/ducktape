"""Tests for the local OAuth callback's HTTP contract."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient, TestServer

from finance.plaid.spend.desktop.oauth import CallbackResult, _callback_app

STATE = "test-oauth-state"
CONTENT_SECURITY_POLICY = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"


def _test_client(callback_future: asyncio.Future[CallbackResult]) -> TestClient:
    return TestClient(TestServer(_callback_app(STATE, callback_future)))


def _assert_response_headers(response: ClientResponse) -> None:
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["Connection"] == "close"


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


if __name__ == "__main__":
    pytest_bazel.main()
