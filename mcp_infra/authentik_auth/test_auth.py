"""Tests for AuthentikAuthConfig, AuthentikTokenExchanger, and ResilientOIDCProxy."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import pytest_bazel
from authlib.integrations.httpx_client import AsyncOAuth2Client as AuthlibAsyncOAuth2Client
from authlib.oauth2 import OAuth2Error
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from glide_shared.exceptions import TimeoutError as GlideTimeoutError
from mcp.server.auth.provider import TokenError
from prometheus_client import REGISTRY
from starlette.exceptions import HTTPException
from tenacity import wait_none

from mcp_infra.authentik_auth.auth import (
    DEFAULT_VALID_SCOPES,
    AuthentikAuthConfig,
    AuthentikTokenExchanger,
    BackendTokenExchangeError,
    DirectJwtTrust,
    ResilientOIDCProxy,
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


def _direct_trust(
    issuer: str = "https://auth.example.com/application/o/machine/",
    audience: str = "machine",
    required_scopes: tuple[str, ...] = ("openid",),
) -> DirectJwtTrust:
    return DirectJwtTrust(issuer=issuer, audiences=(audience,), required_scopes=required_scopes)


@dataclass(frozen=True)
class _AuthBuildHarness:
    http_get: Mock
    oidc_proxy_cls: Mock
    jwt_verifier_cls: Mock
    multi_auth_cls: Mock


@pytest.fixture
def auth_build_harness() -> Generator[_AuthBuildHarness]:
    with (
        patch("mcp_infra.authentik_auth.auth.httpx.get") as http_get,
        patch("mcp_infra.authentik_auth.auth.ResilientOIDCProxy") as oidc_proxy_cls,
        patch("mcp_infra.authentik_auth.auth.JWTVerifier") as jwt_verifier_cls,
        patch("mcp_infra.authentik_auth.auth.MultiAuth") as multi_auth_cls,
    ):
        oidc_proxy_cls.return_value.oidc_config = OIDCConfiguration(
            strict=False, jwks_uri="https://auth.example.com/application/o/test/jwks/"
        )
        yield _AuthBuildHarness(
            http_get=http_get,
            oidc_proxy_cls=oidc_proxy_cls,
            jwt_verifier_cls=jwt_verifier_cls,
            multi_auth_cls=multi_auth_cls,
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


def test_direct_jwt_trust_requires_an_audience() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        DirectJwtTrust(issuer="https://auth.example.com/application/o/machine/", audiences=())


# ── build_authentik_auth tests ────────────────────────────────────────────


def test_build_authentik_auth_uses_proxy_discovery_jwks_uri(auth_build_harness: _AuthBuildHarness) -> None:
    """Direct JWT verification reuses OIDCProxy's validated discovery result."""
    advertised_jwks = "https://auth.example.com/application/o/test/jwks/"
    auth_build_harness.oidc_proxy_cls.return_value.oidc_config = OIDCConfiguration(
        strict=False, jwks_uri=advertised_jwks
    )
    build_authentik_auth(_config().model_copy(update={"direct_jwt_trusts": (_direct_trust(),)}))

    auth_build_harness.http_get.assert_not_called()
    auth_build_harness.jwt_verifier_cls.assert_called_once()
    kwargs = auth_build_harness.jwt_verifier_cls.call_args.kwargs
    assert kwargs["jwks_uri"] == advertised_jwks


def test_build_authentik_auth_sets_default_scopes_through_proxy_api(auth_build_harness: _AuthBuildHarness) -> None:
    build_authentik_auth(_config())

    auth_build_harness.oidc_proxy_cls.return_value.update_default_scopes.assert_called_once_with(DEFAULT_VALID_SCOPES)


def test_build_authentik_auth_sets_custom_scopes_through_proxy_api(auth_build_harness: _AuthBuildHarness) -> None:
    custom_scopes = ["openid", "propose", "read"]
    build_authentik_auth(_config(), valid_scopes=custom_scopes)

    auth_build_harness.oidc_proxy_cls.return_value.update_default_scopes.assert_called_once_with(custom_scopes)


def test_build_authentik_auth_requires_authorization_consent(auth_build_harness: _AuthBuildHarness) -> None:
    build_authentik_auth(_config())

    assert auth_build_harness.oidc_proxy_cls.call_args.kwargs["require_authorization_consent"] is True


def test_build_authentik_auth_accepts_issuer_with_trailing_slash(auth_build_harness: _AuthBuildHarness) -> None:
    """JWTVerifier must accept the issuer both with and without a trailing slash.

    Authentik's per-provider tokens carry `iss` WITH a trailing slash, but
    `normalized_issuer()` strips it and JWTVerifier matches `iss` by exact string,
    so the bare form alone rejects every real Authentik token. Regression: this
    silently blocked all direct machine bearer tokens (e.g. haku → grocy MCP).
    """
    build_authentik_auth(
        _config().model_copy(
            update={"direct_jwt_trusts": (_direct_trust(issuer="https://auth.example.com/application/o/machine/"),)}
        )
    )

    issuer = auth_build_harness.jwt_verifier_cls.call_args.kwargs["issuer"]
    assert "https://auth.example.com/application/o/machine/" in issuer
    assert "https://auth.example.com/application/o/machine" in issuer


def test_build_authentik_auth_keeps_direct_jwt_trusts_separate(auth_build_harness: _AuthBuildHarness) -> None:
    cfg = _config().model_copy(
        update={
            "direct_jwt_trusts": (
                _direct_trust(),
                _direct_trust(
                    issuer="https://auth.example.com/application/o/other-machine/",
                    audience="other-machine",
                    required_scopes=("profile",),
                ),
            )
        }
    )
    build_authentik_auth(cfg)

    assert auth_build_harness.jwt_verifier_cls.call_count == 2
    first, second = (call.kwargs for call in auth_build_harness.jwt_verifier_cls.call_args_list)
    assert first == {
        "jwks_uri": "https://auth.example.com/application/o/test/jwks/",
        "issuer": ["https://auth.example.com/application/o/machine", "https://auth.example.com/application/o/machine/"],
        "audience": ["machine"],
        "required_scopes": ["openid"],
    }
    assert second == {
        "jwks_uri": "https://auth.example.com/application/o/test/jwks/",
        "issuer": [
            "https://auth.example.com/application/o/other-machine",
            "https://auth.example.com/application/o/other-machine/",
        ],
        "audience": ["other-machine"],
        "required_scopes": ["profile"],
    }


def test_build_authentik_auth_appends_extra_verifiers(auth_build_harness: _AuthBuildHarness) -> None:
    sentinel = cast("TokenVerifier", object())
    build_authentik_auth(_config(), extra_verifiers=[sentinel])

    auth_build_harness.jwt_verifier_cls.assert_not_called()
    assert auth_build_harness.multi_auth_cls.call_args.kwargs["verifiers"] == [sentinel]


def test_build_authentik_auth_passes_on_client_authorized(auth_build_harness: _AuthBuildHarness) -> None:
    """on_client_authorized is handed to the OIDCProxy for its pre-token ownership check."""

    async def _cb(client_id: str, idp_tokens: Any) -> None: ...

    build_authentik_auth(_config(), on_client_authorized=_cb)

    assert auth_build_harness.oidc_proxy_cls.call_args.kwargs["on_client_authorized"] is _cb


async def test_resilient_proxy_checks_client_ownership_before_issuing_token() -> None:
    """The ownership hook sees upstream identity before a local client token is issued."""
    proxy = ResilientOIDCProxy.__new__(ResilientOIDCProxy)
    proxy._on_client_authorized = AsyncMock()
    idp_tokens = {"id_token": "jwt-value", "access_token": "at"}
    proxy._code_store = AsyncMock()
    proxy._code_store.get = AsyncMock(return_value=SimpleNamespace(idp_tokens=idp_tokens))
    client = SimpleNamespace(client_id="dcr-xyz")
    auth_code = SimpleNamespace(code="the-code")
    issued = object()

    order: list[str] = []

    async def record_ownership(*_: Any) -> None:
        order.append("ownership")

    async def issue_token(*_: Any) -> object:
        order.append("token")
        return issued

    proxy._on_client_authorized.side_effect = record_ownership
    super_exchange = AsyncMock(side_effect=issue_token)
    with patch.object(OAuthProxy, "exchange_authorization_code", super_exchange) as super_exch:
        result = await ResilientOIDCProxy.exchange_authorization_code(proxy, cast(Any, client), cast(Any, auth_code))

    assert result is issued
    super_exch.assert_awaited_once()
    proxy._on_client_authorized.assert_awaited_once_with("dcr-xyz", idp_tokens)
    assert order == ["ownership", "token"]


async def test_resilient_proxy_restores_dcr_client_id_after_token_swap() -> None:
    """FastMCP's token swap returns the upstream client identity; the override re-attaches the
    DCR client_id from the reference JWT so per-agent identity survives (the "agent
    haku-console-mcp has no linked operator subject" class of failure)."""
    proxy = ResilientOIDCProxy.__new__(ResilientOIDCProxy)
    upstream = AccessToken(token="upstream-at", client_id="upstream-client", scopes=[], expires_at=None)
    proxy._jwt_issuer = cast(Any, SimpleNamespace(verify_token=lambda _t: {"client_id": "dcr-xyz", "jti": "j"}))
    with patch.object(OAuthProxy, "load_access_token", AsyncMock(return_value=upstream)):
        result = await ResilientOIDCProxy.load_access_token(proxy, "fastmcp-jwt")
    assert result is not None
    assert result.client_id == "dcr-xyz"
    assert result.token == "upstream-at"


async def test_resilient_proxy_load_access_token_keeps_non_jwt_identity() -> None:
    """A token super() accepted but the reference-JWT issuer can't verify (a non-JWT verifier
    matched) keeps its identity untouched; a rejected token stays rejected."""
    proxy = ResilientOIDCProxy.__new__(ResilientOIDCProxy)

    def boom(_t: str) -> dict:
        raise ValueError("not a fastmcp jwt")

    proxy._jwt_issuer = cast(Any, SimpleNamespace(verify_token=boom))
    accepted = AccessToken(token="t", client_id="static-agent", scopes=[], expires_at=None)
    with patch.object(OAuthProxy, "load_access_token", AsyncMock(return_value=accepted)):
        result = await ResilientOIDCProxy.load_access_token(proxy, "opaque-bearer")
    assert result is accepted

    with patch.object(OAuthProxy, "load_access_token", AsyncMock(return_value=None)):
        assert await ResilientOIDCProxy.load_access_token(proxy, "bad") is None


# ── AuthentikTokenExchanger tests ─────────────────────────────────────────


def _exchange_config() -> AuthentikAuthConfig:
    return _config(proxy_client_id="proxy-id")


class _FakeOAuthClient:
    def __init__(self, factory: _OAuthClientFactory, *, client_id: str, timeout: float) -> None:
        self.factory = factory
        self.client_id = client_id
        self.timeout = timeout
        self.fetches: list[dict[str, Any]] = []
        self.compliance_hooks: list[tuple[str, Any]] = []
        self.closed = False

    async def __aenter__(self) -> _FakeOAuthClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def fetch_token(self, **kwargs: Any) -> Any:
        self.fetches.append(kwargs)
        effect = self.factory.effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def register_compliance_hook(self, hook_type: str, hook: Any) -> None:
        self.compliance_hooks.append((hook_type, hook))


class _OAuthClientFactory:
    def __init__(self) -> None:
        self.effects: list[Any] = []
        self.clients: list[_FakeOAuthClient] = []

    def __call__(self, *, client_id: str, timeout: float) -> _FakeOAuthClient:
        client = _FakeOAuthClient(self, client_id=client_id, timeout=timeout)
        self.clients.append(client)
        return client


@pytest.fixture
def oauth_client_factory(monkeypatch: pytest.MonkeyPatch) -> _OAuthClientFactory:
    factory = _OAuthClientFactory()
    monkeypatch.setattr("mcp_infra.authentik_auth.auth.AsyncOAuth2Client", factory)
    monkeypatch.setattr(AuthentikTokenExchanger, "exchange_retry_wait", wait_none())
    return factory


def test_token_exchanger_requires_proxy_client_id() -> None:
    with pytest.raises(ValueError, match="proxy_client_id is required"):
        AuthentikTokenExchanger(_config())


async def test_token_exchanger_returns_explicit_result_without_cross_call_state(
    oauth_client_factory: _OAuthClientFactory,
) -> None:
    oauth_client_factory.effects.extend([{"access_token": "backend-1"}, {"access_token": "backend-2"}])
    exchanger = AuthentikTokenExchanger(_exchange_config())
    assert await exchanger.exchange("upstream-authentik-jwt") == "backend-1"
    assert await exchanger.exchange("upstream-authentik-jwt") == "backend-2"

    assert len(oauth_client_factory.clients) == 2
    assert all(client.closed for client in oauth_client_factory.clients)
    for client in oauth_client_factory.clients:
        assert client.client_id == "proxy-id"
        assert client.timeout == 10.0
        assert client.fetches == [
            {
                "url": "https://auth.example.com/application/o/token/",
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": "upstream-authentik-jwt",
                "scope": "openid email profile ak_proxy",
            }
        ]
        assert [hook_type for hook_type, _ in client.compliance_hooks] == ["access_token_response"]


@pytest.mark.parametrize("failure_kind", ["transport", "upstream-5xx"])
async def test_token_exchanger_retries_transient_failure_with_fresh_client(
    oauth_client_factory: _OAuthClientFactory, failure_kind: str
) -> None:
    request = httpx.Request("POST", "https://auth.example.com/application/o/token/")
    failure: BaseException
    if failure_kind == "transport":
        failure = httpx.ConnectError("temporary DNS failure", request=request)
    else:
        failure = httpx.HTTPStatusError("HTTP 503", request=request, response=httpx.Response(503, request=request))
    oauth_client_factory.effects.extend([failure, {"access_token": "backend-after-retry"}])

    assert await AuthentikTokenExchanger(_exchange_config()).exchange("upstream-authentik-jwt") == (
        "backend-after-retry"
    )
    assert len(oauth_client_factory.clients) == 2
    assert all(client.closed for client in oauth_client_factory.clients)


async def test_token_exchanger_retries_real_authlib_429(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def token_endpoint(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429, json={"error": "temporarily_unavailable", "error_description": "rate limited"}, request=request
            )
        return httpx.Response(200, json={"access_token": "backend-after-rate-limit"}, request=request)

    class RealOAuthClient(AuthlibAsyncOAuth2Client):
        def __init__(self, *, client_id: str, timeout: float) -> None:
            super().__init__(client_id=client_id, timeout=timeout, transport=httpx.MockTransport(token_endpoint))

    monkeypatch.setattr("mcp_infra.authentik_auth.auth.AsyncOAuth2Client", RealOAuthClient)
    monkeypatch.setattr(AuthentikTokenExchanger, "exchange_retry_wait", wait_none())

    assert await AuthentikTokenExchanger(_exchange_config()).exchange("upstream-authentik-jwt") == (
        "backend-after-rate-limit"
    )
    assert attempts == 2


@pytest.mark.parametrize("token_data", [None, [], "not-an-object", {}, {"access_token": ""}])
async def test_token_exchanger_sanitizes_malformed_success_response(
    oauth_client_factory: _OAuthClientFactory, token_data: Any
) -> None:
    oauth_client_factory.effects.append(token_data)

    with pytest.raises(BackendTokenExchangeError):
        await AuthentikTokenExchanger(_exchange_config()).exchange("upstream-authentik-jwt")


# ── ResilientOIDCProxy tests ──────────────────────────────────────────────
#
# These pin the load-bearing assumption that FastMCP's OAuthProxy raises its
# blanket TokenError("invalid_grant") `from` the original httpx exception — and
# that our subclass reclassifies transient upstream failures instead of surfacing
# the terminal invalid_grant that makes claude.ai permanently drop the connector.


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> ResilientOIDCProxy:
    # OIDCProxy.__init__ fetches OIDC discovery over the network, so skip it;
    # each test installs the minimum state needed for its branch. Do not wait
    # between retries in tests.
    monkeypatch.setattr(ResilientOIDCProxy, "refresh_retry_wait", wait_none())
    return object.__new__(ResilientOIDCProxy)


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


async def test_resilient_proxy_oauth_state_store_timeout_becomes_503(proxy: ResilientOIDCProxy) -> None:
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


async def test_resilient_proxy_oauth_state_store_timeout_retries_then_succeeds(proxy: ResilientOIDCProxy) -> None:
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
