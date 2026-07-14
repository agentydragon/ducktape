"""Tests for Haku's Agent-facing MCP authentication composition."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_bazel
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import SecretStr
from starlette.exceptions import HTTPException

from haku.console.config import McpOAuthConfig, OperatorOidcConfig, Settings
from haku.console.mcp_agent_auth import (
    OAuthMcpAuth,
    StaticMcpAuth,
    _LinkAgentOperator,
    _VerifiedPrincipalOIDCProxy,
    build_auth,
)
from haku.console.mcp_config import ResolvedStaticAgent
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from mcp_infra.authentik_auth.auth import DEFAULT_VALID_SCOPES, DownstreamClientIdentityOIDCProxy
from mcp_infra.authentik_auth.oidc_principal import (
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)
from mcp_infra.persistence import PostgresPersistence


def _settings(*, mcp_oauth: McpOAuthConfig | None = None) -> Settings:
    return Settings(
        haku_ui_url="https://haku-ui.test",
        public_base_url="https://haku.test",
        database_url=SecretStr("postgresql+psycopg://db.test/haku"),
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.test/application/o/haku-console/",
            client_id="console",
            client_secret=SecretStr("secret"),
            session_secret=SecretStr("session-secret"),
        ),
        mcp_oauth=mcp_oauth,
    )


def _store() -> PostgresMcpOperatorOAuthStore:
    return cast(PostgresMcpOperatorOAuthStore, Mock(spec=PostgresMcpOperatorOAuthStore))


def _static_agent() -> ResolvedStaticAgent:
    return ResolvedStaticAgent(agent="haku", token=SecretStr("agent-token"), operator_subject="operator-42")


def _client(client_id: str = "dcr-claude") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=["https://claude.ai/api/mcp/auth_callback"])


def _authorization_code() -> AuthorizationCode:
    return AuthorizationCode(
        code="downstream-code",
        scopes=["openid"],
        expires_at=4_102_444_800,
        client_id="dcr-claude",
        code_challenge="challenge",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
    )


def _exchange_proxy() -> tuple[_VerifiedPrincipalOIDCProxy, AsyncMock, AsyncMock, SimpleNamespace]:
    proxy = _VerifiedPrincipalOIDCProxy.__new__(_VerifiedPrincipalOIDCProxy)
    resolver = AsyncMock()
    link = AsyncMock()
    code_store = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                client_id="dcr-claude", idp_tokens={"access_token": "upstream-secret", "token_type": "Bearer"}
            )
        ),
        delete=AsyncMock(),
    )
    proxy._principal_resolver = cast(Any, SimpleNamespace(resolve=resolver))
    proxy._on_client_authorized = link
    proxy._code_store = cast(Any, code_store)
    return proxy, resolver, link, code_store


async def test_static_only_auth_maps_bearer_to_namespaced_agent_identity() -> None:
    auth = build_auth(_settings(), [_static_agent()], operator_oauth_store=_store())

    assert isinstance(auth, StaticMcpAuth)
    access = await auth.provider.verify_token("agent-token")
    assert access is not None
    assert access.client_id == "static-agent:haku"


def test_build_auth_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="no configured credential"):
        build_auth(_settings(), [], operator_oauth_store=_store())


async def test_oauth_auth_composes_haku_owned_proxy_storage_static_bearer_and_operator_link() -> None:
    settings = _settings(
        mcp_oauth=McpOAuthConfig(
            oidc_issuer="https://auth.test/application/o/haku-agent/",
            oidc_client_id="haku-agent",
            oidc_client_secret=SecretStr("oauth-secret"),
            persistence=PostgresPersistence(kind="postgres", url="postgresql://db.test/haku"),
        )
    )
    store = _store()
    storage = Mock()
    proxy = Mock(spec=_VerifiedPrincipalOIDCProxy)
    provider = Mock()
    with (
        patch("haku.console.mcp_agent_auth.build_shared_client_storage", return_value=storage),
        patch("haku.console.mcp_agent_auth._VerifiedPrincipalOIDCProxy", return_value=proxy) as proxy_cls,
        patch("haku.console.mcp_agent_auth.compose_authentik_auth", return_value=provider) as compose,
    ):
        auth = build_auth(settings, [_static_agent()], operator_oauth_store=store)

    assert isinstance(auth, OAuthMcpAuth)
    assert auth.provider is provider
    assert auth.storage is storage
    kwargs = proxy_cls.call_args.kwargs
    assert kwargs == {
        "config_url": "https://auth.test/application/o/haku-agent/.well-known/openid-configuration",
        "client_id": "haku-agent",
        "client_secret": "oauth-secret",
        "base_url": "https://haku.test/mcp",
        "client_storage": storage,
        "expected_issuer": "https://auth.test/application/o/haku-agent/",
        "on_client_authorized": kwargs["on_client_authorized"],
    }
    proxy.update_default_scopes.assert_called_once_with(DEFAULT_VALID_SCOPES)
    compose.assert_called_once_with(
        proxy=proxy, direct_jwt_trusts=(), extra_verifiers=compose.call_args.kwargs["extra_verifiers"]
    )
    assert len(compose.call_args.kwargs["extra_verifiers"]) == 1

    await kwargs["on_client_authorized"](
        "dcr-claude", VerifiedOidcPrincipal(issuer="https://auth.test/application/o/haku-agent/", subject="operator-42")
    )
    cast(Mock, store.bind_agent_operator).assert_called_once_with(
        agent_dcr_client_id="dcr-claude", operator_subject="operator-42"
    )


async def test_verified_principal_proxy_verifies_links_then_issues_token() -> None:
    proxy, resolver, link, code_store = _exchange_proxy()
    principal = VerifiedOidcPrincipal(issuer="https://auth.test/application/o/haku-agent/", subject="operator-42")
    token = OAuthToken(access_token="downstream-token")
    events: list[str] = []

    async def resolve(_tokens: object) -> VerifiedOidcPrincipal:
        events.append("verify")
        return principal

    async def bind(_client_id: str, _principal: VerifiedOidcPrincipal) -> None:
        events.append("link")

    async def issue(_client: object, _code: object) -> OAuthToken:
        events.append("issue")
        return token

    resolver.side_effect = resolve
    link.side_effect = bind
    with patch.object(DownstreamClientIdentityOIDCProxy, "exchange_authorization_code", side_effect=issue) as parent:
        result = await proxy.exchange_authorization_code(_client(), _authorization_code())

    assert result == token
    assert events == ["verify", "link", "issue"]
    resolver.assert_awaited_once_with({"access_token": "upstream-secret", "token_type": "Bearer"})
    link.assert_awaited_once_with("dcr-claude", principal)
    parent.assert_awaited_once()
    code_store.get.assert_awaited_once_with(key="downstream-code")


async def test_verified_principal_proxy_consumes_code_for_terminal_invalid_principal() -> None:
    proxy, resolver, link, code_store = _exchange_proxy()
    resolver.side_effect = InvalidOidcPrincipalError()
    with (
        patch.object(
            DownstreamClientIdentityOIDCProxy, "exchange_authorization_code", new_callable=AsyncMock
        ) as parent,
        pytest.raises(TokenError) as raised,
    ):
        await proxy.exchange_authorization_code(_client(), _authorization_code())

    assert raised.value.error == "invalid_grant"
    assert raised.value.error_description == "Authorization grant identity is invalid."
    assert "upstream-secret" not in raised.value.error_description
    link.assert_not_awaited()
    parent.assert_not_awaited()
    code_store.delete.assert_awaited_once_with(key="downstream-code")


async def test_verified_principal_proxy_rejects_code_owned_by_another_client() -> None:
    proxy, resolver, link, code_store = _exchange_proxy()
    with (
        patch.object(
            DownstreamClientIdentityOIDCProxy, "exchange_authorization_code", new_callable=AsyncMock
        ) as parent,
        pytest.raises(TokenError) as raised,
    ):
        await proxy.exchange_authorization_code(_client("other-client"), _authorization_code())

    assert raised.value.error == "invalid_grant"
    resolver.assert_not_awaited()
    link.assert_not_awaited()
    parent.assert_not_awaited()
    code_store.delete.assert_not_awaited()


async def test_verified_principal_proxy_rejects_authorization_code_for_another_client() -> None:
    proxy, resolver, link, code_store = _exchange_proxy()
    authorization_code = _authorization_code().model_copy(update={"client_id": "other-client"})
    with (
        patch.object(
            DownstreamClientIdentityOIDCProxy, "exchange_authorization_code", new_callable=AsyncMock
        ) as parent,
        pytest.raises(TokenError) as raised,
    ):
        await proxy.exchange_authorization_code(_client(), authorization_code)

    assert raised.value.error == "invalid_grant"
    resolver.assert_not_awaited()
    link.assert_not_awaited()
    parent.assert_not_awaited()
    code_store.delete.assert_not_awaited()


async def test_verified_principal_proxy_consumes_code_when_operator_link_is_rejected() -> None:
    proxy, resolver, _link, code_store = _exchange_proxy()
    principal = VerifiedOidcPrincipal(issuer="https://auth.test/application/o/haku-agent/", subject="new-operator")
    resolver.return_value = principal
    store = _store()
    cast(Mock, store.bind_agent_operator).side_effect = ValueError("already bound to a different operator")
    proxy._on_client_authorized = _LinkAgentOperator(store)
    with (
        patch.object(
            DownstreamClientIdentityOIDCProxy, "exchange_authorization_code", new_callable=AsyncMock
        ) as parent,
        pytest.raises(TokenError) as raised,
    ):
        await proxy.exchange_authorization_code(_client(), _authorization_code())

    assert raised.value.error == "invalid_grant"
    assert raised.value.error_description == "Authorization grant identity is invalid."
    code_store.delete.assert_awaited_once_with(key="downstream-code")
    parent.assert_not_awaited()


async def test_verified_principal_proxy_leaves_code_retryable_when_jwks_is_unavailable() -> None:
    proxy, resolver, link, code_store = _exchange_proxy()
    principal = VerifiedOidcPrincipal(issuer="https://auth.test/application/o/haku-agent/", subject="operator-42")
    resolver.side_effect = [OidcPrincipalVerificationUnavailableError(), principal]
    token = OAuthToken(access_token="downstream-token")
    with patch.object(
        DownstreamClientIdentityOIDCProxy, "exchange_authorization_code", new_callable=AsyncMock, return_value=token
    ) as parent:
        with pytest.raises(HTTPException) as unavailable:
            await proxy.exchange_authorization_code(_client(), _authorization_code())
        result = await proxy.exchange_authorization_code(_client(), _authorization_code())

    assert unavailable.value.status_code == 503
    assert unavailable.value.headers == {"Retry-After": "60"}
    assert result == token
    assert code_store.get.await_count == 2
    link.assert_awaited_once_with("dcr-claude", principal)
    parent.assert_awaited_once()
    code_store.delete.assert_not_awaited()


if __name__ == "__main__":
    pytest_bazel.main()
