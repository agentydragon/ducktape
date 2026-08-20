"""Tests for Haku's MCP authentication composition."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock, call, patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastmcp.server.auth.auth import AccessToken
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentAuthorization,
    StaticAgentDefinition,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.chat_models import RuntimeKind, SessionStatus
from haku.console.config import McpOAuthConfig, OperatorIdentityConfig, OperatorOidcConfig, Settings
from haku.console.conftest import console_sessions, operator_id
from haku.console.database_schema import Agent, Conversation, Operator, Session
from haku.console.mcp_agent_auth import OAuthMcpAuth, StaticAgentCredentialRegistry, StaticMcpAuth, build_auth
from haku.console.mcp_auth.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    BearerVerificationUnavailableError,
    HakuAgentOAuthProxy,
    HakuFailurePreservingMultiAuth,
)
from haku.console.operator_identity import OperatorStatus
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES
from mcp_infra.persistence import PostgresPersistence

_AGENT_ID = UUID("10000000-0000-4000-8000-000000000001")
_BINDING_ID = UUID("20000000-0000-4000-8000-000000000002")
_OPERATOR_ID = UUID("30000000-0000-4000-8000-000000000003")
_DATABASE_URL = "postgresql+psycopg://db.test/haku"
_TOKEN = "agent-token"
_TOKEN_FINGERPRINT = fingerprint_static_token(_TOKEN)


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
        config_file=Path("/unused/haku-console.yaml"),
    )


def _authority() -> PostgresAgentAuthority:
    authority = cast(PostgresAgentAuthority, Mock(spec=PostgresAgentAuthority))
    cast(AsyncMock, authority.static_authorization_for_fingerprint).return_value = StaticAgentAuthorization(
        agent_id=_AGENT_ID, binding_id=_BINDING_ID, operator_id=_OPERATOR_ID
    )
    return authority


def _credentials(*tokens: str) -> StaticAgentCredentialRegistry:
    return StaticAgentCredentialRegistry(fingerprints=tuple(fingerprint_static_token(token) for token in tokens))


def _identity_store() -> PostgresOperatorIdentityStore:
    return cast(PostgresOperatorIdentityStore, Mock(spec=PostgresOperatorIdentityStore))


def _oauth_proxy() -> Mock:
    proxy = Mock(spec=HakuAgentOAuthProxy)
    proxy.base_url = None
    proxy.resource_base_url = None
    proxy.required_scopes = []
    cast(AsyncMock, proxy.verify_token).return_value = None
    return proxy


def _static_auth(authority: PostgresAgentAuthority | None = None) -> StaticMcpAuth:
    auth = build_auth(
        _settings(),
        agent_authority=authority or _authority(),
        static_credentials=_credentials(_TOKEN),
        operator_identity_store=_identity_store(),
    )
    assert isinstance(auth, StaticMcpAuth)
    return auth


def _mcp_oauth_config() -> McpOAuthConfig:
    return McpOAuthConfig(
        oidc_issuer="https://auth.test/application/o/haku-agent/",
        oidc_client_id="haku-agent",
        oidc_client_secret=SecretStr("oauth-secret"),
        persistence=PostgresPersistence(kind="postgres", url=_DATABASE_URL),
    )


@dataclass(frozen=True)
class _OAuthAuthHarness:
    auth: OAuthMcpAuth
    authority: PostgresAgentAuthority
    storage: Mock
    proxy: Mock
    proxy_class: Mock


def _oauth_auth(*static_tokens: str) -> _OAuthAuthHarness:
    authority = _authority()
    storage = Mock()
    proxy = _oauth_proxy()
    with (
        patch("haku.console.mcp_agent_auth.build_shared_client_storage", return_value=storage),
        patch("haku.console.mcp_agent_auth.HakuAgentOAuthProxy", return_value=proxy) as proxy_class,
    ):
        auth = build_auth(
            _settings(mcp_oauth=_mcp_oauth_config()),
            agent_authority=authority,
            static_credentials=_credentials(*static_tokens),
            operator_identity_store=_identity_store(),
        )
    assert isinstance(auth, OAuthMcpAuth)
    return _OAuthAuthHarness(auth=auth, authority=authority, storage=storage, proxy=proxy, proxy_class=proxy_class)


async def test_static_auth_resolves_the_exact_active_binding_actor() -> None:
    authority = _authority()
    auth = _static_auth(authority)

    assert isinstance(auth.provider, HakuFailurePreservingMultiAuth)
    access = await auth.provider.verify_token(_TOKEN)
    assert access is not None
    assert access.client_id == f"haku-static-binding:{_BINDING_ID}"
    assert await auth.static_actor_resolver.resolve_static_actor(access) == AgentActor(
        agent_id=_AGENT_ID, operator_id=_OPERATOR_ID, binding_id=_BINDING_ID
    )
    cast(AsyncMock, authority.static_authorization_for_fingerprint).assert_has_awaits(
        [
            call(fingerprint=_TOKEN_FINGERPRINT, record_seen=False),
            call(fingerprint=_TOKEN_FINGERPRINT, record_seen=True),
        ]
    )


async def test_static_auth_rejects_unconfigured_and_inactive_credentials() -> None:
    authority = _authority()
    auth = _static_auth(authority)

    assert await auth.provider.verify_token("not-configured") is None
    cast(AsyncMock, authority.static_authorization_for_fingerprint).assert_not_awaited()

    cast(AsyncMock, authority.static_authorization_for_fingerprint).side_effect = StaticAgentRejectedError()
    assert await auth.provider.verify_token(_TOKEN) is None


async def test_static_actor_resolution_rejects_forged_binding_evidence() -> None:
    auth = _static_auth()

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
    auth = _static_auth(authority)

    with pytest.raises(BearerVerificationUnavailableError, match="temporarily unavailable"):
        await auth.provider.verify_token(_TOKEN)


def test_build_auth_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="no configured credential"):
        build_auth(
            _settings(),
            agent_authority=_authority(),
            static_credentials=_credentials(),
            operator_identity_store=_identity_store(),
        )


async def test_session_bearer_resolves_the_pinned_agent_profile_and_session(
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
) -> None:
    resolved_operator_id = await operator_id(migrated_sessions, "session-agent-operator")
    agent_id = uuid4()
    binding_token = "configured-agent-token"
    session_token = "session-rendezvous-token"
    authority = PostgresAgentAuthority(
        console_sessions(migrated_db_url),
        public_base_url="https://haku.test",
        operator_identity_store=migrated_identity_store,
        access_profiles=("pinned",),
        default_access_profile_id="pinned",
    )
    authorization = (
        await authority.reconcile_static_agents(
            [
                StaticAgentDefinition(
                    agent_id=agent_id,
                    display_name="Session Agent",
                    operator_id=resolved_operator_id,
                    secret_reference="env:SESSION_AGENT_TOKEN",
                    token_fingerprint=fingerprint_static_token(binding_token),
                    access_profile_id="pinned",
                )
            ]
        )
    )[0]
    conversation_id, session_id = uuid4(), uuid4()
    now = datetime.datetime.now(datetime.UTC)
    async with migrated_sessions.begin() as db:
        db.add(
            Conversation(
                conversation_id=conversation_id,
                operator_id=resolved_operator_id,
                agent_id=agent_id,
                access_profile_id="pinned",
                runtime_kind=RuntimeKind.CLAUDE_CODE,
                created_at=now,
            )
        )
        db.add(
            Session(
                session_id=session_id,
                operator_id=resolved_operator_id,
                conversation_id=conversation_id,
                agent_binding_id=authorization.binding_id,
                status=SessionStatus.READY,
                bridge_token_fingerprint=fingerprint_static_token(session_token),
                bridge_connected_at=now,
                lease_expires_at=now + datetime.timedelta(minutes=1),
                lease_holder="test-replica",
                created_at=now,
                updated_at=now,
            )
        )

    auth = build_auth(
        _settings(),
        agent_authority=authority,
        static_credentials=_credentials(binding_token),
        operator_identity_store=_identity_store(),
        session_tokens=migrated_sessions,
    )
    assert isinstance(auth, StaticMcpAuth)
    access = await auth.provider.verify_token(session_token)
    assert access is not None
    assert access.client_id == f"haku-chat-session:{session_id}"
    assert await auth.static_actor_resolver.resolve_static_actor(access) == AgentActor(
        agent_id=agent_id,
        operator_id=resolved_operator_id,
        binding_id=authorization.binding_id,
        access_profile_id="pinned",
        session_id=session_id,
    )

    # The bearer is a live-session credential, not merely a durable fingerprint lookup. It is
    # unusable before runner attachment, while the runner lease is expired, or after durable Agent
    # authority failure. The conversation's access profile is an immutable launch snapshot, so a
    # later Agent profile edit does not retarget the running session; explicit credential rotation
    # below does revoke that generation.
    async with migrated_sessions.begin() as db:
        row = await db.get(Session, session_id)
        assert row is not None
        row.status = SessionStatus.PROVISIONING
    assert await auth.provider.verify_token(session_token) is None

    async with migrated_sessions.begin() as db:
        row = await db.get(Session, session_id)
        assert row is not None
        row.status = SessionStatus.READY
        row.bridge_connected_at = None
    assert await auth.provider.verify_token(session_token) is None

    async with migrated_sessions.begin() as db:
        row = await db.get(Session, session_id)
        assert row is not None
        row.bridge_connected_at = now
        row.lease_expires_at = now - datetime.timedelta(seconds=1)
    assert await auth.provider.verify_token(session_token) is None

    async with migrated_sessions.begin() as db:
        row = await db.get(Session, session_id)
        assert row is not None
        row.lease_expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=1)
        agent = await db.get(Agent, agent_id)
        assert agent is not None
        agent.access_profile_id = "drifted"
    drifted_access = await auth.provider.verify_token(session_token)
    assert drifted_access is not None
    assert await auth.static_actor_resolver.resolve_static_actor(drifted_access) == AgentActor(
        agent_id=agent_id,
        operator_id=resolved_operator_id,
        binding_id=authorization.binding_id,
        access_profile_id="pinned",
        session_id=session_id,
    )

    async with migrated_sessions.begin() as db:
        operator = await db.get(Operator, resolved_operator_id)
        assert operator is not None
        operator.status = OperatorStatus.DISABLED
    assert await auth.provider.verify_token(session_token) is None

    async with migrated_sessions.begin() as db:
        operator = await db.get(Operator, resolved_operator_id)
        assert operator is not None
        operator.status = OperatorStatus.ACTIVE
    assert await auth.provider.verify_token(session_token) is not None

    # The session is pinned to the credential generation that authorized its launch. Rotating that
    # binding is an explicit revocation of the still-running sandbox, not silent re-attribution to
    # the successor credential.
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=agent_id,
                display_name="Session Agent",
                operator_id=resolved_operator_id,
                secret_reference="env:SESSION_AGENT_TOKEN",
                token_fingerprint=fingerprint_static_token("rotated-configured-agent-token"),
                access_profile_id="pinned",
            )
        ]
    )
    assert await auth.provider.verify_token(session_token) is None


async def test_session_bearer_is_rejected_after_its_session_ends(
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
) -> None:
    resolved_operator_id = await operator_id(migrated_sessions, "ended-session-agent-operator")
    agent_id = uuid4()
    session_token = "ended-session-token"
    authority = PostgresAgentAuthority(
        console_sessions(migrated_db_url),
        public_base_url="https://haku.test",
        operator_identity_store=migrated_identity_store,
        access_profiles=("pinned",),
        default_access_profile_id="pinned",
    )
    authorization = (
        await authority.reconcile_static_agents(
            [
                StaticAgentDefinition(
                    agent_id=agent_id,
                    display_name="Ended Session Agent",
                    operator_id=resolved_operator_id,
                    secret_reference="env:ENDED_SESSION_AGENT_TOKEN",
                    token_fingerprint=fingerprint_static_token("ended-agent-token"),
                    access_profile_id="pinned",
                )
            ]
        )
    )[0]
    now = datetime.datetime.now(datetime.UTC)
    conversation_id, session_id = uuid4(), uuid4()
    async with migrated_sessions.begin() as db:
        db.add(
            Conversation(
                conversation_id=conversation_id,
                operator_id=resolved_operator_id,
                agent_id=agent_id,
                access_profile_id="pinned",
                runtime_kind=RuntimeKind.CLAUDE_CODE,
                created_at=now,
            )
        )
        db.add(
            Session(
                session_id=session_id,
                operator_id=resolved_operator_id,
                conversation_id=conversation_id,
                agent_binding_id=authorization.binding_id,
                status=SessionStatus.CLOSED,
                bridge_token_fingerprint=fingerprint_static_token(session_token),
                bridge_connected_at=now,
                lease_expires_at=now + datetime.timedelta(minutes=1),
                lease_holder="test-replica",
                created_at=now,
                updated_at=now,
            )
        )
    auth = build_auth(
        _settings(),
        agent_authority=authority,
        static_credentials=_credentials(),
        operator_identity_store=_identity_store(),
        session_tokens=migrated_sessions,
    )
    assert await auth.provider.verify_token(session_token) is None


async def test_oauth_auth_composes_one_authority_storage_and_optional_static_verifier() -> None:
    harness = _oauth_auth(_TOKEN)
    auth = harness.auth

    assert isinstance(auth.provider, HakuFailurePreservingMultiAuth)
    assert auth.storage is harness.storage
    assert auth.static_actor_resolver is not None
    harness.proxy_class.assert_called_once_with(
        config_url="https://auth.test/application/o/haku-agent/.well-known/openid-configuration",
        client_id="haku-agent",
        client_secret="oauth-secret",
        base_url="https://haku.test/mcp",
        resource_base_url="https://haku.test",
        client_storage=harness.storage,
        expected_issuer="https://auth.test/application/o/haku-agent/",
        grant_authority=harness.authority,
    )
    harness.proxy.update_default_scopes.assert_called_once_with(DEFAULT_VALID_SCOPES)

    access = await auth.provider.verify_token(_TOKEN)
    assert access is not None
    assert access.client_id == f"haku-static-binding:{_BINDING_ID}"


def test_oauth_auth_does_not_invent_a_static_resolver_without_static_credentials() -> None:
    auth = _oauth_auth().auth

    assert auth.static_actor_resolver is None


if __name__ == "__main__":
    pytest_bazel.main()
