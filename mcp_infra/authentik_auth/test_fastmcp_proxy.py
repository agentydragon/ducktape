"""Tests for FastMCP proxy compatibility and refresh resilience."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_bazel
from authlib.oauth2 import OAuth2Error
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from glide_shared.exceptions import TimeoutError as GlideTimeoutError
from mcp.server.auth.provider import TokenError
from prometheus_client import REGISTRY
from starlette.exceptions import HTTPException
from tenacity import wait_none

from mcp_infra.authentik_auth.fastmcp_proxy import (
    DownstreamClientIdentityOIDCProxy,
    RetryableJWTVerifier,
    RetryableRefreshOIDCProxy,
)


async def test_downstream_identity_proxy_restores_dcr_client_id_after_token_swap() -> None:
    """FastMCP's token swap returns the upstream client identity; the override re-attaches the
    DCR client_id from the reference JWT so per-agent identity survives (the "agent
    haku-console-mcp has no linked operator subject" class of failure)."""
    proxy = DownstreamClientIdentityOIDCProxy.__new__(DownstreamClientIdentityOIDCProxy)
    upstream = AccessToken(token="upstream-at", client_id="upstream-client", scopes=[], expires_at=None)
    proxy._jwt_issuer = cast(Any, SimpleNamespace(verify_token=lambda _t: {"client_id": "dcr-xyz", "jti": "j"}))
    with patch.object(OAuthProxy, "load_access_token", AsyncMock(return_value=upstream)):
        result = await DownstreamClientIdentityOIDCProxy.load_access_token(proxy, "fastmcp-jwt")
    assert result is not None
    assert result.client_id == "dcr-xyz"
    assert result.token == "upstream-at"


async def test_downstream_identity_proxy_keeps_non_jwt_identity() -> None:
    """A token super() accepted but the reference-JWT issuer can't verify (a non-JWT verifier
    matched) keeps its identity untouched; a rejected token stays rejected."""
    proxy = DownstreamClientIdentityOIDCProxy.__new__(DownstreamClientIdentityOIDCProxy)

    def boom(_t: str) -> dict:
        raise ValueError("not a fastmcp jwt")

    proxy._jwt_issuer = cast(Any, SimpleNamespace(verify_token=boom))
    accepted = AccessToken(token="t", client_id="static-agent", scopes=[], expires_at=None)
    with patch.object(OAuthProxy, "load_access_token", AsyncMock(return_value=accepted)):
        result = await DownstreamClientIdentityOIDCProxy.load_access_token(proxy, "opaque-bearer")
    assert result is accepted

    with patch.object(OAuthProxy, "load_access_token", AsyncMock(return_value=None)):
        assert await DownstreamClientIdentityOIDCProxy.load_access_token(proxy, "bad") is None


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> RetryableRefreshOIDCProxy:
    # OIDCProxy.__init__ fetches OIDC discovery over the network, so skip it;
    # each test installs the minimum state needed for its branch. Do not wait
    # between retries in tests.
    monkeypatch.setattr(RetryableRefreshOIDCProxy, "refresh_retry_wait", wait_none())
    return object.__new__(RetryableRefreshOIDCProxy)


def _invalid_grant_from(cause: BaseException | None) -> TokenError:
    # Synthetic helper for focused wrapper tests. TokenError is a frozen
    # dataclass, so `raise ... from` is needed to set its interpreter-level cause.
    try:
        raise TokenError("invalid_grant", "Upstream refresh failed: boom") from cause
    except TokenError as raised:
        return raised


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://auth.example.com/application/o/token/")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


def _failures(outcome: str) -> float:
    return REGISTRY.get_sample_value("mcp_auth_upstream_refresh_failures_total", {"outcome": outcome}) or 0.0


async def test_retryable_refresh_proxy_transient_5xx_becomes_503(proxy: RetryableRefreshOIDCProxy) -> None:
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


async def test_retryable_refresh_proxy_dns_failure_becomes_503(proxy: RetryableRefreshOIDCProxy) -> None:
    """DNS resolution failure (cluster DNS outage) is transient, not invalid_grant."""
    dns_error = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    with (
        patch.object(OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=_invalid_grant_from(dns_error))),
        pytest.raises(HTTPException) as exc_info,
    ):
        await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), [])
    assert exc_info.value.status_code == 503


async def test_retryable_refresh_proxy_retries_then_succeeds(proxy: RetryableRefreshOIDCProxy) -> None:
    """A blip on the first attempt is absorbed by the in-process retry."""
    token = object()
    with patch.object(
        OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=[_invalid_grant_from(_http_error(503)), token])
    ) as upstream:
        assert await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), []) is token
    assert upstream.call_count == 2


async def test_retryable_refresh_proxy_oauth_state_store_timeout_becomes_503(proxy: RetryableRefreshOIDCProxy) -> None:
    """A Valkey/glide timeout while persisting rotated OAuth state is transient.

    This is the path seen in grocy-sf logs: Authentik returned 200, then
    fastmcp timed out writing the refreshed upstream token to Valkey. It should
    not leak as a raw 500 to claude.ai.
    """
    before = _failures("storage")
    with (
        patch.object(
            OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=GlideTimeoutError("timed out"))
        ) as upstream,
        pytest.raises(HTTPException) as exc_info,
    ):
        await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), [])
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers is not None
    assert "Retry-After" in exc_info.value.headers
    assert upstream.call_count == 3
    assert _failures("storage") == before + 1


async def test_retryable_refresh_proxy_oauth_state_store_timeout_retries_then_succeeds(
    proxy: RetryableRefreshOIDCProxy,
) -> None:
    token = object()
    with patch.object(
        OIDCProxy, "exchange_refresh_token", AsyncMock(side_effect=[GlideTimeoutError("timed out"), token])
    ) as upstream:
        assert await proxy.exchange_refresh_token(AsyncMock(), AsyncMock(), []) is token
    assert upstream.call_count == 2


@pytest.mark.parametrize(
    "upstream_rejection",
    [_http_error(400), OAuth2Error(description="Token is invalid or expired")],
    ids=["http-4xx", "authlib-oauth2error"],
)
async def test_retryable_refresh_proxy_genuine_oauth_error_stays_invalid_grant(
    proxy: RetryableRefreshOIDCProxy, upstream_rejection: Exception
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


async def test_retryable_refresh_proxy_local_token_errors_pass_through(proxy: RetryableRefreshOIDCProxy) -> None:
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


def test_get_token_verifier_returns_retryable_jwt_verifier(proxy: RetryableRefreshOIDCProxy) -> None:
    """The per-request verifier FastMCP builds during __init__ must be the retrying
    subclass, or JWKS-fetch blips go on 401ing every request during the blip."""
    oidc_config = SimpleNamespace(jwks_uri="https://auth.example.com/jwks", issuer="https://auth.example.com")
    proxy.oidc_config = cast(Any, oidc_config)
    verifier = proxy.get_token_verifier()
    assert isinstance(verifier, RetryableJWTVerifier)


@pytest.fixture
def jwt_verifier(monkeypatch: pytest.MonkeyPatch) -> RetryableJWTVerifier:
    monkeypatch.setattr(RetryableJWTVerifier, "jwks_retry_wait", wait_none())
    return RetryableJWTVerifier(jwks_uri="https://auth.example.com/jwks", issuer="https://auth.example.com")


async def test_retryable_jwt_verifier_retries_transient_jwks_fetch_then_succeeds(
    jwt_verifier: RetryableJWTVerifier,
) -> None:
    """A blip on the first JWKS fetch is absorbed by the in-process retry, so
    load_access_token never sees it and can't collapse it into a plain None."""
    jwks: dict[str, list[Any]] = {"keys": []}
    with patch.object(JWTVerifier, "_fetch_jwks", AsyncMock(side_effect=[_http_error(503), jwks])) as upstream:
        assert await jwt_verifier._fetch_jwks() is jwks
    assert upstream.call_count == 2


async def test_retryable_jwt_verifier_dns_failure_retries_then_gives_up(jwt_verifier: RetryableJWTVerifier) -> None:
    """A persistent transient failure still raises after exhausting attempts —
    it's not silently absorbed forever, just given a few chances to clear."""
    dns_error = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    with (
        patch.object(JWTVerifier, "_fetch_jwks", AsyncMock(side_effect=dns_error)) as upstream,
        pytest.raises(httpx.ConnectError),
    ):
        await jwt_verifier._fetch_jwks()
    assert upstream.call_count == 3  # jwks_retry_stop = stop_after_attempt(3)


async def test_retryable_jwt_verifier_genuine_4xx_is_not_retried(jwt_verifier: RetryableJWTVerifier) -> None:
    """A non-5xx HTTP error from the JWKS endpoint says something about JWKS
    config, not upstream flakiness — no point retrying it."""
    with (
        patch.object(JWTVerifier, "_fetch_jwks", AsyncMock(side_effect=_http_error(404))) as upstream,
        pytest.raises(httpx.HTTPStatusError),
    ):
        await jwt_verifier._fetch_jwks()
    assert upstream.call_count == 1


if __name__ == "__main__":
    pytest_bazel.main()
