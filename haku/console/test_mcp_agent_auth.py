"""Tests for Haku's Agent-facing MCP authentication composition."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
import pytest_bazel
from fastmcp.server.auth.auth import AccessToken
from pydantic import SecretStr

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentAuthorization,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.config import McpOAuthConfig, OperatorIdentityConfig, OperatorOidcConfig, Settings
from haku.console.mcp_agent_auth import OAuthMcpAuth, StaticAgentCredentialRegistry, StaticMcpAuth, build_auth
from haku.console.mcp_auth.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    BearerVerificationUnavailableError,
    HakuAgentOAuthProxy,
    HakuFailurePreservingMultiAuth,
)
from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES
from mcp_infra.persistence import PostgresPersistence

_AGENT_ID = UUID("10000000-0000-4000-8000-000000000001")
_BINDING_ID = UUID("20000000-0000-4000-8000-000000000002")
_OPERATOR_ID = UUID("30000000-0000-4000-8000-000000000003")
_DATABASE_URL = "postgresql+psycopg://db.test/haku"
_TOKEN = "agent-token"


def _settings(*, mcp_oauth: McpOAuthConfig | None = None) -> Settings:
    return Settings(
        haku_ui_url="https://haku-ui.test",
        public_base_url="https://haku.test",
        database_url=SecretStr(_DATABASE_URL),
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.test/application/o/haku-console/",
            client_id="console",
            client_secret=SecretStr("secret"),
            session_secret=SecretStr("session-secret"),
        ),
        operator_identity=OperatorIdentityConfig(trust_domain="auth.test/authentik-user-id/v1"),
        mcp_oauth=mcp_oauth,
    )


def _authority() -> PostgresAgentAuthority:
    authority = cast(PostgresAgentAuthority, Mock(spec=PostgresAgentAuthority))
    cast(AsyncMock, authority.static_authorization_for_fingerprint).return_value = StaticAgentAuthorization(
        agent_id=_AGENT_ID, binding_id=_BINDING_ID, operator_id=_OPERATOR_ID
    )
    return authority


def _credentials(*tokens: str) -> StaticAgentCredentialRegistry:
    return StaticAgentCredentialRegistry(fingerprints=tuple(fingerprint_static_token(token) for token in tokens))


async def test_static_auth_resolves_the_exact_active_binding_actor() -> None:
    authority = _authority()
    auth = build_auth(_settings(), agent_authority=authority, static_credentials=_credentials(_TOKEN))

    assert isinstance(auth, StaticMcpAuth)
    assert isinstance(auth.provider, HakuFailurePreservingMultiAuth)
    access = await auth.provider.verify_token(_TOKEN)
    assert access is not None
    assert access.client_id == f"haku-static-binding:{_BINDING_ID}"
    assert await auth.static_actor_resolver.resolve_static_actor(access) == AgentActor(
        agent_id=_AGENT_ID, operator_id=_OPERATOR_ID, binding_id=_BINDING_ID
    )
    assert cast(AsyncMock, authority.static_authorization_for_fingerprint).await_count == 2
    cast(AsyncMock, authority.static_authorization_for_fingerprint).assert_awaited_with(
        fingerprint=fingerprint_static_token(_TOKEN)
    )


async def test_static_auth_rejects_unconfigured_and_inactive_credentials() -> None:
    authority = _authority()
    auth = build_auth(_settings(), agent_authority=authority, static_credentials=_credentials(_TOKEN))

    assert await auth.provider.verify_token("not-configured") is None
    cast(AsyncMock, authority.static_authorization_for_fingerprint).assert_not_awaited()

    cast(AsyncMock, authority.static_authorization_for_fingerprint).side_effect = StaticAgentRejectedError()
    assert await auth.provider.verify_token(_TOKEN) is None


async def test_static_actor_resolution_rejects_forged_binding_evidence() -> None:
    auth = build_auth(_settings(), agent_authority=_authority(), static_credentials=_credentials(_TOKEN))
    assert isinstance(auth, StaticMcpAuth)

    forged = AccessToken(
        token=_TOKEN,
        client_id="haku-static-binding:40000000-0000-4000-8000-000000000004",
        scopes=[],
        expires_at=None,
        claims={},
    )
    assert await auth.static_actor_resolver.resolve_static_actor(forged) is None


async def test_static_verification_preserves_authority_outages() -> None:
    authority = _authority()
    cast(AsyncMock, authority.static_authorization_for_fingerprint).side_effect = AgentGrantAuthorityUnavailableError()
    auth = build_auth(_settings(), agent_authority=authority, static_credentials=_credentials(_TOKEN))

    with pytest.raises(BearerVerificationUnavailableError, match="temporarily unavailable"):
        await auth.provider.verify_token(_TOKEN)


def test_build_auth_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="no configured credential"):
        build_auth(_settings(), agent_authority=_authority(), static_credentials=_credentials())


async def test_oauth_auth_composes_one_authority_storage_and_optional_static_verifier() -> None:
    oauth = McpOAuthConfig(
        oidc_issuer="https://auth.test/application/o/haku-agent/",
        oidc_client_id="haku-agent",
        oidc_client_secret=SecretStr("oauth-secret"),
        persistence=PostgresPersistence(kind="postgres", url=_DATABASE_URL),
    )
    authority = _authority()
    storage = Mock()
    proxy = Mock(spec=HakuAgentOAuthProxy)
    cast(AsyncMock, proxy.verify_token).return_value = None
    with (
        patch("haku.console.mcp_agent_auth.build_shared_client_storage", return_value=storage),
        patch("haku.console.mcp_agent_auth.HakuAgentOAuthProxy", return_value=proxy) as proxy_class,
    ):
        auth = build_auth(
            _settings(mcp_oauth=oauth), agent_authority=authority, static_credentials=_credentials(_TOKEN)
        )

    assert isinstance(auth, OAuthMcpAuth)
    assert isinstance(auth.provider, HakuFailurePreservingMultiAuth)
    assert auth.storage is storage
    assert auth.static_actor_resolver is not None
    proxy_class.assert_called_once_with(
        config_url="https://auth.test/application/o/haku-agent/.well-known/openid-configuration",
        client_id="haku-agent",
        client_secret="oauth-secret",
        base_url="https://haku.test/mcp",
        client_storage=storage,
        expected_issuer="https://auth.test/application/o/haku-agent/",
        grant_authority=authority,
    )
    proxy.update_default_scopes.assert_called_once_with(DEFAULT_VALID_SCOPES)

    access = await auth.provider.verify_token(_TOKEN)
    assert access is not None
    assert access.client_id == f"haku-static-binding:{_BINDING_ID}"


def test_oauth_auth_does_not_invent_a_static_resolver_without_static_credentials() -> None:
    oauth = McpOAuthConfig(
        oidc_issuer="https://auth.test/application/o/haku-agent/",
        oidc_client_id="haku-agent",
        oidc_client_secret=SecretStr("oauth-secret"),
        persistence=PostgresPersistence(kind="postgres", url=_DATABASE_URL),
    )
    with (
        patch("haku.console.mcp_agent_auth.build_shared_client_storage", return_value=Mock()),
        patch("haku.console.mcp_agent_auth.HakuAgentOAuthProxy", return_value=Mock(spec=HakuAgentOAuthProxy)),
    ):
        auth = build_auth(_settings(mcp_oauth=oauth), agent_authority=_authority(), static_credentials=_credentials())

    assert isinstance(auth, OAuthMcpAuth)
    assert auth.static_actor_resolver is None


if __name__ == "__main__":
    pytest_bazel.main()
