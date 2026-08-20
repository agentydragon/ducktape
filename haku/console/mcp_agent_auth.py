"""Compose Agent bearer and Operator browser-session authentication for Haku's MCP server."""

from __future__ import annotations

import datetime
import hmac
from dataclasses import dataclass
from uuid import UUID

from fastmcp.server.auth.auth import AccessToken, AuthProvider, TokenVerifier
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import HTTPConnection

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentAuthorization,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.chat_models import SessionStatus
from haku.console.config import MCP_PATH, Settings
from haku.console.database_schema import Conversation, Session
from haku.console.mcp_auth.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    BearerVerificationUnavailableError,
    HakuAgentOAuthProxy,
    HakuFailurePreservingMultiAuth,
    OperatorSessionAuthenticationError,
    StaticAgentActorResolver,
    assert_fastmcp_adapter_compatibility,
)
from haku.console.operator_auth import operator_session_for_identity_store
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import AgentActor, OperatorActor
from haku.console.x.launch_identity import LaunchAgentRejectedError
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES
from mcp_infra.persistence import OAuthClientStorage, build_shared_client_storage

_STATIC_BINDING_CLIENT_ID_PREFIX = "haku-static-binding:"
_CHAT_SESSION_CLIENT_ID_PREFIX = "haku-chat-session:"
_MCP_SESSION_STATUSES = (SessionStatus.READY, SessionStatus.RESPONDING)


@dataclass(frozen=True, slots=True)
class StaticAgentCredentialRegistry:
    """Configured static credential fingerprints, without retaining raw bearers."""

    fingerprints: tuple[bytes, ...]

    def configured_fingerprint(self, token: str) -> bytes | None:
        try:
            presented = fingerprint_static_token(token)
        except ValueError:
            return None
        return next(
            (fingerprint for fingerprint in self.fingerprints if hmac.compare_digest(presented, fingerprint)), None
        )


class _AuthorityStaticTokenVerifier(TokenVerifier, StaticAgentActorResolver):
    """Verify and resolve a static bearer through the canonical binding aggregate."""

    def __init__(self, authority: PostgresAgentAuthority, credentials: StaticAgentCredentialRegistry) -> None:
        super().__init__()
        self._authority = authority
        self._credentials = credentials

    async def verify_token(self, token: str) -> AccessToken | None:
        authorization = await self._authorization(token, verification=True)
        if authorization is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"{_STATIC_BINDING_CLIENT_ID_PREFIX}{authorization.binding_id}",
            scopes=[],
            expires_at=None,
            claims={},
        )

    async def resolve_static_actor(self, access_token: AccessToken) -> AgentActor | None:
        authorization = await self._authorization(access_token.token, verification=False)
        if authorization is None:
            return None
        if access_token.client_id != f"{_STATIC_BINDING_CLIENT_ID_PREFIX}{authorization.binding_id}":
            return None
        return AgentActor(
            agent_id=authorization.agent_id,
            operator_id=authorization.operator_id,
            binding_id=authorization.binding_id,
            access_profile_id=authorization.access_profile_id,
        )

    async def _authorization(self, token: str, *, verification: bool) -> StaticAgentAuthorization | None:
        try:
            fingerprint = self._credentials.configured_fingerprint(token)
            if fingerprint is None:
                return None
            return await self._authority.static_authorization_for_fingerprint(
                fingerprint=fingerprint, record_seen=not verification
            )
        except (StaticAgentRejectedError, ValueError):
            return None
        except AgentGrantAuthorityUnavailableError as error:
            if verification:
                raise BearerVerificationUnavailableError("Agent authorization is temporarily unavailable") from error
            raise


@dataclass(frozen=True, slots=True)
class _SessionAgentAuthorization:
    session_id: UUID
    agent_id: UUID
    binding_id: UUID
    operator_id: UUID
    access_profile_id: str


class _AuthoritySessionTokenVerifier(TokenVerifier, StaticAgentActorResolver):
    """Resolve a Console-minted sandbox bearer to its immutable session identity."""

    def __init__(self, authority: PostgresAgentAuthority, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._authority = authority
        self._sessions = sessions

    async def verify_token(self, token: str) -> AccessToken | None:
        authorization = await self._authorization(token)
        if authorization is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"{_CHAT_SESSION_CLIENT_ID_PREFIX}{authorization.session_id}",
            scopes=[],
            expires_at=None,
            claims={},
        )

    async def resolve_static_actor(self, access_token: AccessToken) -> AgentActor | None:
        if not access_token.client_id.startswith(_CHAT_SESSION_CLIENT_ID_PREFIX):
            return None
        authorization = await self._authorization(access_token.token)
        if authorization is None:
            return None
        if access_token.client_id != f"{_CHAT_SESSION_CLIENT_ID_PREFIX}{authorization.session_id}":
            return None
        return AgentActor(
            agent_id=authorization.agent_id,
            operator_id=authorization.operator_id,
            binding_id=authorization.binding_id,
            access_profile_id=authorization.access_profile_id,
            session_id=authorization.session_id,
        )

    async def _authorization(self, token: str) -> _SessionAgentAuthorization | None:
        try:
            fingerprint = fingerprint_static_token(token)
        except ValueError:
            return None
        now = datetime.datetime.now(datetime.UTC)
        try:
            async with self._sessions.begin() as db:
                row = (
                    await db.execute(
                        select(
                            Session.session_id,
                            Session.operator_id,
                            Session.agent_binding_id,
                            Conversation.agent_id,
                            Conversation.access_profile_id,
                        )
                        .join(Conversation, Conversation.conversation_id == Session.conversation_id)
                        .where(
                            Session.bridge_token_fingerprint == fingerprint,
                            Session.status.in_(_MCP_SESSION_STATUSES),
                            Session.bridge_connected_at.is_not(None),
                            Session.lease_expires_at.is_not(None),
                            Session.lease_expires_at > now,
                        )
                    )
                ).one_or_none()
                if row is None or row.agent_binding_id is None or row.agent_id is None or row.access_profile_id is None:
                    return None
                active = await self._authority.launch_authorization(
                    operator_id=row.operator_id,
                    agent_id=row.agent_id,
                    access_profile_id=row.access_profile_id,
                    binding_id=row.agent_binding_id,
                    db=db,
                )
                return _SessionAgentAuthorization(
                    session_id=row.session_id,
                    agent_id=active.agent_id,
                    binding_id=active.binding_id,
                    operator_id=active.operator_id,
                    access_profile_id=row.access_profile_id,
                )
        except (LaunchAgentRejectedError, ValueError):
            return None
        except AgentGrantAuthorityUnavailableError as error:
            raise BearerVerificationUnavailableError("Agent authorization is temporarily unavailable") from error
        except SQLAlchemyError as error:
            raise BearerVerificationUnavailableError("session authorization is temporarily unavailable") from error


class _CompositeAgentActorResolver(StaticAgentActorResolver):
    def __init__(self, resolvers: tuple[StaticAgentActorResolver, ...]) -> None:
        self._resolvers = resolvers

    async def resolve_static_actor(self, access_token: AccessToken) -> AgentActor | None:
        for resolver in self._resolvers:
            if (actor := await resolver.resolve_static_actor(access_token)) is not None:
                return actor
        return None


@dataclass(frozen=True)
class StaticMcpAuth:
    provider: AuthProvider
    static_actor_resolver: StaticAgentActorResolver


@dataclass(frozen=True)
class OAuthMcpAuth:
    provider: AuthProvider
    storage: OAuthClientStorage
    static_actor_resolver: StaticAgentActorResolver | None


type McpAuth = StaticMcpAuth | OAuthMcpAuth


class _OperatorMcpSessionAuthenticator:
    """Turn the console's DB-revalidated browser session into an MCP Operator principal."""

    def __init__(self, settings: Settings, identity_store: PostgresOperatorIdentityStore) -> None:
        self._mcp_path = MCP_PATH
        self._public_origin = settings.public_base_url.rstrip("/")
        self._identity_store = identity_store

    async def __call__(self, conn: HTTPConnection) -> OperatorActor | None:
        if conn.url.path != self._mcp_path:
            return None
        session = await operator_session_for_identity_store(conn, self._identity_store)
        if session is None:
            return None
        if conn.headers.get("origin") != self._public_origin:
            raise OperatorSessionAuthenticationError("operator MCP requests require the console's exact Origin")
        return OperatorActor(operator_id=session.operator_id)


def build_auth(
    settings: Settings,
    *,
    agent_authority: PostgresAgentAuthority,
    static_credentials: StaticAgentCredentialRegistry,
    operator_identity_store: PostgresOperatorIdentityStore,
    session_tokens: async_sessionmaker[AsyncSession] | None = None,
) -> McpAuth:
    """Compose FastMCP protocol auth with Haku's canonical Agent authority.

    FastMCP owns DCR, PKCE, callback, client, and token-family storage. Haku's OAuth adapter
    delegates every product authorization decision to ``agent_authority``. Static credentials use
    the same authority and are accepted only when their exact fingerprint-backed binding is active.
    """
    assert_fastmcp_adapter_compatibility()
    static = (
        _AuthorityStaticTokenVerifier(agent_authority, static_credentials) if static_credentials.fingerprints else None
    )
    session = _AuthoritySessionTokenVerifier(agent_authority, session_tokens) if session_tokens is not None else None
    token_verifiers: tuple[TokenVerifier, ...] = tuple(
        verifier for verifier in (static, session) if verifier is not None
    )
    actor_resolvers: tuple[StaticAgentActorResolver, ...] = tuple(
        resolver for resolver in (static, session) if resolver is not None
    )
    actor_resolver: StaticAgentActorResolver | None = (
        _CompositeAgentActorResolver(actor_resolvers) if actor_resolvers else None
    )
    operator_session_authenticator = _OperatorMcpSessionAuthenticator(settings, operator_identity_store)
    if settings.mcp_oauth is not None:
        storage = build_shared_client_storage(settings.mcp_oauth.persistence)
        config = settings.mcp_oauth.as_authentik_auth_config(public_base_url=settings.public_base_url)
        proxy = HakuAgentOAuthProxy(
            config_url=f"{config.normalized_issuer()}/.well-known/openid-configuration",
            client_id=config.oidc_client_id,
            client_secret=config.oidc_client_secret,
            base_url=config.normalized_public_base_url(),
            resource_base_url=settings.public_base_url,
            client_storage=storage,
            expected_issuer=config.oidc_issuer,
            grant_authority=agent_authority,
        )
        proxy.update_default_scopes(DEFAULT_VALID_SCOPES)
        return OAuthMcpAuth(
            provider=HakuFailurePreservingMultiAuth(
                server=proxy,
                verifiers=list(token_verifiers),
                operator_session_authenticator=operator_session_authenticator,
            ),
            storage=storage,
            static_actor_resolver=actor_resolver,
        )
    if not token_verifiers:
        raise ValueError(
            "haku-console /mcp has no configured credential: set at least one static Agent "
            "(config_file `static_agents`) or `mcp_oauth`"
        )
    assert actor_resolver is not None
    primary, *additional = token_verifiers
    return StaticMcpAuth(
        provider=HakuFailurePreservingMultiAuth(
            server=primary, verifiers=additional, operator_session_authenticator=operator_session_authenticator
        ),
        static_actor_resolver=actor_resolver,
    )
