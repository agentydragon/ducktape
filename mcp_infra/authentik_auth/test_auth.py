"""Tests for AuthentikAuthConfig, AuthentikExchangeAuth, and ResilientOIDCProxy."""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_bazel
from authlib.oauth2 import OAuth2Error
from authlib.oauth2.rfc6749.wrappers import OAuth2Token
from fastmcp.server.auth.auth import TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import TokenError
from prometheus_client import REGISTRY
from starlette.exceptions import HTTPException
from tenacity import wait_none

from mcp_infra.authentik_auth.auth import (
    _EXCHANGE_TOKEN_COLLECTION,
    _EXPIRY_LEEWAY,
    AuthentikAuthConfig,
    AuthentikExchangeAuth,
    ResilientOIDCProxy,
    _cache_key,
    build_authentik_auth,
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


# ── build_authentik_auth tests ────────────────────────────────────────────


def test_build_authentik_auth_uses_jwks_uri_from_discovery() -> None:
    """JWTVerifier must receive the jwks_uri advertised by the OIDC discovery
    doc — not a hand-built path. Authentik serves JWKS at `<issuer>/jwks/`,
    not `<issuer>/.well-known/jwks`; baking the wrong URL into auth.py once
    silently broke JWT validation on every /mcp call.
    """
    advertised_jwks = "https://auth.example.com/application/o/test/jwks/"
    discovery_doc = {
        "issuer": "https://auth.example.com/application/o/test/",
        "jwks_uri": advertised_jwks,
        "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
        "token_endpoint": "https://auth.example.com/application/o/token/",
    }

    discovery_response = AsyncMock()
    discovery_response.raise_for_status = lambda: discovery_response
    discovery_response.json = lambda: discovery_doc

    with (
        patch("mcp_infra.authentik_auth.auth.httpx.get", return_value=discovery_response) as http_get,
        patch("mcp_infra.authentik_auth.auth.ResilientOIDCProxy") as oidc_proxy_cls,
        patch("mcp_infra.authentik_auth.auth.JWTVerifier") as jwt_verifier_cls,
        patch("mcp_infra.authentik_auth.auth.MultiAuth"),
    ):
        oidc_proxy_cls.return_value.client_registration_options = AsyncMock()
        build_authentik_auth(_config())

    assert any("/.well-known/openid-configuration" in str(call.args[0]) for call in http_get.call_args_list)
    jwt_verifier_cls.assert_called_once()
    kwargs = jwt_verifier_cls.call_args.kwargs
    assert kwargs["jwks_uri"] == advertised_jwks, (
        f"JWTVerifier got hand-built jwks_uri {kwargs['jwks_uri']!r} instead of the "
        f"discovery-advertised {advertised_jwks!r}"
    )


def test_build_authentik_auth_accepts_issuer_with_trailing_slash() -> None:
    """JWTVerifier must accept the issuer both with and without a trailing slash.

    Authentik's per-provider tokens carry `iss` WITH a trailing slash, but
    `normalized_issuer()` strips it and JWTVerifier matches `iss` by exact string,
    so the bare form alone rejects every real Authentik token. Regression: this
    silently blocked all direct machine bearer tokens (e.g. haku → grocy MCP).
    """
    discovery_response = AsyncMock()
    discovery_response.raise_for_status = lambda: discovery_response
    discovery_response.json = lambda: {"jwks_uri": "https://auth.example.com/application/o/test/jwks/"}

    with (
        patch("mcp_infra.authentik_auth.auth.httpx.get", return_value=discovery_response),
        patch("mcp_infra.authentik_auth.auth.ResilientOIDCProxy") as oidc_proxy_cls,
        patch("mcp_infra.authentik_auth.auth.JWTVerifier") as jwt_verifier_cls,
        patch("mcp_infra.authentik_auth.auth.MultiAuth"),
    ):
        oidc_proxy_cls.return_value.client_registration_options = AsyncMock()
        build_authentik_auth(_config(issuer="https://auth.example.com/application/o/test/"))

    issuer = jwt_verifier_cls.call_args.kwargs["issuer"]
    assert "https://auth.example.com/application/o/test/" in issuer  # the form Authentik actually emits
    assert "https://auth.example.com/application/o/test" in issuer


def test_build_authentik_auth_includes_extra_jwt_issuers() -> None:
    """extra_jwt_issuers widen the JWTVerifier's accepted issuers (both slash forms),
    so a sibling provider sharing the signing key (e.g. a dedicated machine
    client_credentials provider) is accepted on the same MCP.
    """
    discovery_response = AsyncMock()
    discovery_response.raise_for_status = lambda: discovery_response
    discovery_response.json = lambda: {"jwks_uri": "https://auth.example.com/application/o/test/jwks/"}

    cfg = _config(issuer="https://auth.example.com/application/o/test/").model_copy(
        update={"extra_jwt_issuers": ("https://auth.example.com/application/o/machine/",)}
    )
    with (
        patch("mcp_infra.authentik_auth.auth.httpx.get", return_value=discovery_response),
        patch("mcp_infra.authentik_auth.auth.ResilientOIDCProxy") as oidc_proxy_cls,
        patch("mcp_infra.authentik_auth.auth.JWTVerifier") as jwt_verifier_cls,
        patch("mcp_infra.authentik_auth.auth.MultiAuth"),
    ):
        oidc_proxy_cls.return_value.client_registration_options = AsyncMock()
        build_authentik_auth(cfg)

    issuer = jwt_verifier_cls.call_args.kwargs["issuer"]
    assert "https://auth.example.com/application/o/machine/" in issuer
    assert "https://auth.example.com/application/o/machine" in issuer
    assert "https://auth.example.com/application/o/test/" in issuer  # primary still present


def test_build_authentik_auth_appends_extra_verifiers() -> None:
    """extra_verifiers ride the MultiAuth after the Authentik JWTVerifier, so a
    machine caller's StaticTokenVerifier is accepted on the same endpoint as the
    human OAuth flow (gmail-labeling's Haku bearer + operator OAuth).
    """
    discovery_response = AsyncMock()
    discovery_response.raise_for_status = lambda: discovery_response
    discovery_response.json = lambda: {"jwks_uri": "https://auth.example.com/application/o/test/jwks/"}

    sentinel = cast("TokenVerifier", object())
    with (
        patch("mcp_infra.authentik_auth.auth.httpx.get", return_value=discovery_response),
        patch("mcp_infra.authentik_auth.auth.ResilientOIDCProxy") as oidc_proxy_cls,
        patch("mcp_infra.authentik_auth.auth.JWTVerifier") as jwt_verifier_cls,
        patch("mcp_infra.authentik_auth.auth.MultiAuth") as multi_auth_cls,
    ):
        oidc_proxy_cls.return_value.client_registration_options = AsyncMock()
        build_authentik_auth(_config(), extra_verifiers=[sentinel])

    verifiers = multi_auth_cls.call_args.kwargs["verifiers"]
    assert verifiers == [jwt_verifier_cls.return_value, sentinel]  # JWT first, extra appended


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


# ── ResilientOIDCProxy tests ──────────────────────────────────────────────
#
# These pin the load-bearing assumption that fastmcp's OAuthProxy raises its
# blanket TokenError("invalid_grant") `from` the original httpx exception
# (fastmcp 3.1.0 oauth_proxy/proxy.py exchange_refresh_token) — and that our
# subclass reclassifies transient upstream failures instead of surfacing the
# terminal invalid_grant that makes claude.ai permanently drop the connector.


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> ResilientOIDCProxy:
    # exchange_refresh_token (the code under test) only calls super() and module
    # globals; OIDCProxy.__init__ would fetch the OIDC discovery URL over the
    # network, so skip it. No waiting between retries in tests.
    monkeypatch.setattr(ResilientOIDCProxy, "refresh_retry_wait", wait_none())
    return object.__new__(ResilientOIDCProxy)


def _invalid_grant_from(cause: BaseException | None) -> TokenError:
    # TokenError is a frozen dataclass — __cause__ can't be assigned directly.
    # `raise ... from` sets it at the interpreter level, exactly like fastmcp does.
    try:
        raise TokenError("invalid_grant", "Upstream refresh failed: boom") from cause
    except TokenError as raised:
        return raised


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://auth.example.com/application/o/token/")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


def _failures(outcome: str) -> float:
    return REGISTRY.get_sample_value("mcp_auth_upstream_refresh_failures_total", {"outcome": outcome}) or 0.0


async def test_resilient_proxy_transient_5xx_becomes_503(proxy: ResilientOIDCProxy) -> None:
    """Authentik down (gateway 503) must NOT surface as invalid_grant."""
    before = _failures("transient")
    with (
        patch.object(
            OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=_invalid_grant_from(_http_error(503)))
        ) as upstream,
        pytest.raises(HTTPException) as exc_info,
    ):
        await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), [])
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers is not None
    assert "Retry-After" in exc_info.value.headers
    assert upstream.call_count == 3  # stop_after_attempt(3)
    assert _failures("transient") == before + 1


async def test_resilient_proxy_dns_failure_becomes_503(proxy: ResilientOIDCProxy) -> None:
    """DNS resolution failure (cluster DNS outage) is transient, not invalid_grant."""
    dns_error = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    with (
        patch.object(OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=_invalid_grant_from(dns_error))),
        pytest.raises(HTTPException) as exc_info,
    ):
        await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), [])
    assert exc_info.value.status_code == 503


async def test_resilient_proxy_retries_then_succeeds(proxy: ResilientOIDCProxy) -> None:
    """A blip on the first attempt is absorbed by the in-process retry."""
    token = object()
    with patch.object(
        OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=[_invalid_grant_from(_http_error(503)), token])
    ) as upstream:
        assert await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), []) is token
    assert upstream.call_count == 2


@pytest.mark.parametrize(
    "upstream_rejection",
    [_http_error(400), OAuth2Error(description="Token is invalid or expired")],
    ids=["http-4xx", "authlib-oauth2error"],
)
async def test_resilient_proxy_genuine_oauth_error_stays_invalid_grant(
    proxy: ResilientOIDCProxy, upstream_rejection: Exception
) -> None:
    """Authentik actually rejecting the grant (4xx / OAuth error response) must
    still surface as invalid_grant so the client knows to re-authenticate."""
    before = _failures("oauth")
    with (
        patch.object(
            OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=_invalid_grant_from(upstream_rejection))
        ) as upstream,
        pytest.raises(TokenError),
    ):
        await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), [])
    assert upstream.call_count == 1  # no retry for genuine rejections
    assert _failures("oauth") == before + 1


async def test_resilient_proxy_local_token_errors_pass_through(proxy: ResilientOIDCProxy) -> None:
    """TokenErrors with no upstream cause (unknown refresh token, missing JTI
    mapping) are local invalid_grant — re-raised untouched, no retry, and NOT
    counted as an upstream failure (they'd false-fire the alert)."""
    before = _failures("oauth")
    with (
        patch.object(OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=_invalid_grant_from(None))) as upstream,
        pytest.raises(TokenError),
    ):
        await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), [])
    assert upstream.call_count == 1
    assert _failures("oauth") == before  # local churn never reached Authentik


if __name__ == "__main__":
    pytest_bazel.main()
