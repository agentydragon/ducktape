"""Tests for AuthentikAuthConfig and AuthentikExchangeAuth."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel
from authlib.oauth2.rfc6749.wrappers import OAuth2Token
from key_value.aio.stores.memory import MemoryStore

from mcp_infra.authentik_auth.auth import (
    _EXCHANGE_TOKEN_COLLECTION,
    _EXPIRY_LEEWAY,
    AuthentikAuthConfig,
    AuthentikExchangeAuth,
    _cache_key,
)

# ── AuthentikAuthConfig tests ─────────────────────────────────────────────


def _config(
    issuer: str = "https://auth.example.com/application/o/test/",
    public_base_url: str = "https://mcp.example.com",
    proxy_client_id: str | None = None,
) -> AuthentikAuthConfig:
    return AuthentikAuthConfig(
        oidc_issuer=issuer,
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url=public_base_url,
        proxy_client_id=proxy_client_id,
    )


def test_token_endpoint_simple() -> None:
    assert _config().authentik_token_endpoint() == "https://auth.example.com/application/o/token/"


def test_token_endpoint_preserves_reverse_proxy_prefix() -> None:
    cfg = _config("https://example.com/auth/application/o/test/")
    assert cfg.authentik_token_endpoint() == "https://example.com/auth/application/o/token/"


def test_token_endpoint_accepts_unterminated_issuer() -> None:
    cfg = _config("https://auth.example.com/application/o/test")
    assert cfg.authentik_token_endpoint() == "https://auth.example.com/application/o/token/"


def test_token_endpoint_rejects_non_authentik_issuer() -> None:
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        _config("https://keycloak.example.com/realms/test").authentik_token_endpoint()


def test_token_endpoint_rejects_missing_slug() -> None:
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        _config("https://auth.example.com/application/o/").authentik_token_endpoint()


def test_normalized_public_base_url_strips_trailing_slash() -> None:
    cfg = _config(public_base_url="https://mcp.example.com/")
    assert cfg.normalized_public_base_url() == "https://mcp.example.com"


def test_proxy_client_id_optional() -> None:
    cfg = _config()
    assert cfg.proxy_client_id is None


# ── AuthentikExchangeAuth tests ───────────────────────────────────────────


def _exchange_config() -> AuthentikAuthConfig:
    return _config(proxy_client_id="proxy-id")


def _make_token(access_token: str = "exchanged-jwt", expires_in: int = 3600) -> OAuth2Token:
    return OAuth2Token({"access_token": access_token, "expires_in": expires_in, "token_type": "bearer"})


def test_exchange_auth_requires_proxy_client_id() -> None:
    with pytest.raises(ValueError, match="proxy_client_id is required"):
        AuthentikExchangeAuth(_config())


async def test_exchange_auth_fetches_and_caches_token() -> None:
    auth = AuthentikExchangeAuth(_exchange_config())
    mock_token = _make_token()

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock, return_value=mock_token) as fetch:
        # First call: cache miss → fetch.
        token = await auth._get_exchanged_token("upstream-jwt-1")
        assert token == "exchanged-jwt"
        assert fetch.call_count == 1

        # Second call with same upstream token: cache hit → no fetch.
        token = await auth._get_exchanged_token("upstream-jwt-1")
        assert token == "exchanged-jwt"
        assert fetch.call_count == 1

        # Different upstream token: cache miss → fetch again.
        mock_token2 = _make_token(access_token="exchanged-jwt-2")
        fetch.return_value = mock_token2
        token = await auth._get_exchanged_token("upstream-jwt-2")
        assert token == "exchanged-jwt-2"
        assert fetch.call_count == 2

    await auth.aclose()


async def test_exchange_auth_refetches_expired_token() -> None:
    auth = AuthentikExchangeAuth(_exchange_config())

    # Token that expires immediately (expires_at in the past).
    expired_token = OAuth2Token({"access_token": "old", "expires_at": int(time.time()) - 1, "token_type": "bearer"})
    fresh_token = _make_token(access_token="fresh")

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock) as fetch:
        fetch.return_value = expired_token
        token = await auth._get_exchanged_token("upstream")
        assert token == "old"
        assert fetch.call_count == 1

        # Token is expired → should re-fetch.
        fetch.return_value = fresh_token
        token = await auth._get_exchanged_token("upstream")
        assert token == "fresh"
        assert fetch.call_count == 2

    await auth.aclose()


async def test_exchange_auth_respects_leeway() -> None:
    auth = AuthentikExchangeAuth(_exchange_config())

    # Token that expires within the leeway window — should be treated as expired.
    almost_expired = OAuth2Token(
        {"access_token": "almost", "expires_at": int(time.time()) + _EXPIRY_LEEWAY - 1, "token_type": "bearer"}
    )
    fresh = _make_token(access_token="fresh")

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock) as fetch:
        fetch.return_value = almost_expired
        await auth._get_exchanged_token("upstream")

        fetch.return_value = fresh
        token = await auth._get_exchanged_token("upstream")
        assert token == "fresh"
        assert fetch.call_count == 2

    await auth.aclose()


# ── Token store persistence tests ─────────────────────────────────────────


async def test_exchange_auth_persists_to_store() -> None:
    store = MemoryStore()
    auth = AuthentikExchangeAuth(_exchange_config(), token_store=store)
    mock_token = _make_token()

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock, return_value=mock_token):
        await auth._get_exchanged_token("upstream-jwt")

    # Verify token was persisted.
    key = _cache_key("upstream-jwt")
    stored = await store.get(key, collection=_EXCHANGE_TOKEN_COLLECTION)
    assert stored is not None
    assert stored["access_token"] == "exchanged-jwt"

    await auth.aclose()


async def test_exchange_auth_restores_from_store() -> None:
    store = MemoryStore()
    upstream = "upstream-jwt"
    key = _cache_key(upstream)

    # Pre-populate the store with a valid token.
    token_data: dict[str, Any] = {
        "access_token": "stored-jwt",
        "expires_at": int(time.time()) + 3600,
        "expires_in": 3600,
        "token_type": "bearer",
    }
    await store.put(key, token_data, collection=_EXCHANGE_TOKEN_COLLECTION)

    auth = AuthentikExchangeAuth(_exchange_config(), token_store=store)

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock) as fetch:
        token = await auth._get_exchanged_token(upstream)
        assert token == "stored-jwt"
        assert fetch.call_count == 0

    await auth.aclose()


async def test_exchange_auth_ignores_expired_store_entry() -> None:
    store = MemoryStore()
    upstream = "upstream-jwt"
    key = _cache_key(upstream)

    # Pre-populate with an expired token.
    expired_data: dict[str, Any] = {
        "access_token": "stale-jwt",
        "expires_at": int(time.time()) - 1,
        "token_type": "bearer",
    }
    await store.put(key, expired_data, collection=_EXCHANGE_TOKEN_COLLECTION)

    auth = AuthentikExchangeAuth(_exchange_config(), token_store=store)
    fresh = _make_token(access_token="fresh-jwt")

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock, return_value=fresh) as fetch:
        token = await auth._get_exchanged_token(upstream)
        assert token == "fresh-jwt"
        assert fetch.call_count == 1

    await auth.aclose()


async def test_exchange_auth_works_without_store() -> None:
    """Backward compat: no store → pure in-memory caching."""
    auth = AuthentikExchangeAuth(_exchange_config())
    assert auth._token_store is None
    mock_token = _make_token()

    with patch.object(auth._exchange_client, "fetch_token", new_callable=AsyncMock, return_value=mock_token) as fetch:
        token = await auth._get_exchanged_token("upstream")
        assert token == "exchanged-jwt"
        assert fetch.call_count == 1

        # Cache hit.
        token = await auth._get_exchanged_token("upstream")
        assert token == "exchanged-jwt"
        assert fetch.call_count == 1

    await auth.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
