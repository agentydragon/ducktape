"""Tests for tana.token_broker.broker."""

import base64
import hashlib
import time

import httpx
import pytest_bazel
import respx
from httpx import ConnectError, Response

from tana.token_broker.broker import (
    TokenResult,
    _compute_code_challenge,
    _exchange_code,
    _generate_code_verifier,
    _refresh_token,
    _register_client,
    _token_needs_refresh,
    _wait_for_tana,
)

TANA_URL = "http://127.0.0.1:8262"


def test_code_verifier_length() -> None:
    verifier = _generate_code_verifier()
    assert len(verifier) == 128


def test_code_verifier_uniqueness() -> None:
    v1 = _generate_code_verifier()
    v2 = _generate_code_verifier()
    assert v1 != v2


def test_code_challenge_s256() -> None:
    verifier = "test-verifier-value"
    challenge = _compute_code_challenge(verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected


@respx.mock
async def test_wait_for_tana_immediate() -> None:
    """Tana responds healthy on first try."""
    respx.get(f"{TANA_URL}/health").mock(return_value=Response(200))
    _wait_for_tana.retry.wait = lambda *a, **kw: 0
    async with httpx.AsyncClient() as client:
        await _wait_for_tana(client, TANA_URL)


@respx.mock
async def test_wait_for_tana_retries() -> None:
    """Tana fails twice then succeeds."""
    route = respx.get(f"{TANA_URL}/health")
    route.side_effect = [ConnectError("refused"), Response(503), Response(200)]
    _wait_for_tana.retry.wait = lambda *a, **kw: 0
    async with httpx.AsyncClient() as client:
        await _wait_for_tana(client, TANA_URL)
    assert route.call_count == 3


@respx.mock
async def test_register_client() -> None:
    respx.post(f"{TANA_URL}/oauth/register").mock(return_value=Response(200, json={"client_id": "cid_abc123"}))
    async with httpx.AsyncClient() as client:
        client_id = await _register_client(client, TANA_URL, "http://127.0.0.1:9876/callback")
    assert client_id == "cid_abc123"


@respx.mock
async def test_exchange_code() -> None:
    respx.post(f"{TANA_URL}/oauth/token").mock(
        return_value=Response(
            200,
            json={
                "access_token": "at_test123",
                "refresh_token": "rt_test456",
                "token_type": "Bearer",
                "expires_in": 14400,
            },
        )
    )
    async with httpx.AsyncClient() as client:
        token = await _exchange_code(
            client,
            TANA_URL,
            client_id="cid_abc",
            auth_code="ac_xyz",
            code_verifier="verifier123",
            redirect_uri="http://127.0.0.1:9876/callback",
        )
    assert token.access_token == "at_test123"
    assert token.refresh_token == "rt_test456"
    assert token.token_type == "Bearer"
    assert token.client_id == "cid_abc"
    assert int(token.expires_at) > time.time()


@respx.mock
async def test_refresh_token() -> None:
    respx.post(f"{TANA_URL}/oauth/token").mock(
        return_value=Response(
            200, json={"access_token": "at_refreshed", "refresh_token": "rt_new", "expires_in": 14400}
        )
    )
    async with httpx.AsyncClient() as client:
        token = await _refresh_token(client, TANA_URL, "cid_abc", "rt_old")
    assert token.access_token == "at_refreshed"
    assert token.refresh_token == "rt_new"


@respx.mock
async def test_refresh_preserves_old_refresh_token() -> None:
    """If server omits refresh_token, keep the old one."""
    respx.post(f"{TANA_URL}/oauth/token").mock(
        return_value=Response(200, json={"access_token": "at_new", "expires_in": 14400})
    )
    async with httpx.AsyncClient() as client:
        token = await _refresh_token(client, TANA_URL, "cid_abc", "rt_precious")
    assert token.refresh_token == "rt_precious"


def test_token_needs_refresh_not_yet() -> None:
    token = TokenResult(
        access_token="a", refresh_token="r", token_type="Bearer", expires_at=str(int(time.time()) + 7200), client_id="c"
    )
    assert not _token_needs_refresh(token, margin_seconds=3600)


def test_token_needs_refresh_soon() -> None:
    token = TokenResult(
        access_token="a", refresh_token="r", token_type="Bearer", expires_at=str(int(time.time()) + 1800), client_id="c"
    )
    assert _token_needs_refresh(token, margin_seconds=3600)


def test_token_needs_refresh_expired() -> None:
    token = TokenResult(
        access_token="a", refresh_token="r", token_type="Bearer", expires_at=str(int(time.time()) - 100), client_id="c"
    )
    assert _token_needs_refresh(token, margin_seconds=3600)


def test_token_needs_refresh_invalid_expires_at() -> None:
    token = TokenResult(access_token="a", refresh_token="r", token_type="Bearer", expires_at="invalid", client_id="c")
    assert _token_needs_refresh(token, margin_seconds=3600)


if __name__ == "__main__":
    pytest_bazel.main()
