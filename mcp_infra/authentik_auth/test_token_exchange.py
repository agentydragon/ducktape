"""Tests for request-scoped Authentik backend token exchange."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_bazel
from authlib.integrations.httpx_client import AsyncOAuth2Client as AuthlibAsyncOAuth2Client
from tenacity import wait_none

from mcp_infra.authentik_auth.config import AuthentikAuthConfig
from mcp_infra.authentik_auth.token_exchange import AuthentikTokenExchanger, BackendTokenExchangeError


def _config(
    issuer: str = "https://auth.example.com/application/o/test/", proxy_client_id: str | None = None
) -> AuthentikAuthConfig:
    return AuthentikAuthConfig(
        oidc_issuer=issuer,
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url="https://mcp.example.com",
        proxy_client_id=proxy_client_id,
    )


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
    monkeypatch.setattr("mcp_infra.authentik_auth.token_exchange.AsyncOAuth2Client", factory)
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

    monkeypatch.setattr("mcp_infra.authentik_auth.token_exchange.AsyncOAuth2Client", RealOAuthClient)
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


if __name__ == "__main__":
    pytest_bazel.main()
