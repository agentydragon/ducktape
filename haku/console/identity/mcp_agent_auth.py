"""Compose Agent bearer and Operator browser-session authentication for Haku's MCP server."""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp.server.auth.auth import AccessToken, AuthProvider, TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import HTTPConnection

from haku.console.config import MCP_PATH
from haku.console.identity.agent_bearer_authority import (
    AgentBearerAuthority,
    StaticAgentCredentialRegistry,
    build_agent_bearer_authority,
)
from haku.console.identity.authorization import PostgresAgentAuthority
from haku.console.identity.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    BearerVerificationUnavailableError,
    HakuAgentOAuthProxy,
    HakuFailurePreservingMultiAuth,
    OperatorSessionAuthenticationError,
    StaticAgentActorResolver,
    ensure_supported_fastmcp_version,
)
from haku.console.identity.operator_auth import operator_session_for_identity_store
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.settings import Settings
from haku.console.tool_call_actor import AgentActor, OperatorActor
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES
from mcp_infra.persistence import OAuthClientStorage, build_shared_client_storage


class _AgentBearerTokenVerifier(TokenVerifier, StaticAgentActorResolver):
    """Adapt generic Agent bearer authority to FastMCP's token contracts."""

    def __init__(self, authority: AgentBearerAuthority) -> None:
        super().__init__()
        self._authority = authority

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            resolved = await self._authority.resolve(token)
        except AgentGrantAuthorityUnavailableError as error:
            raise BearerVerificationUnavailableError("Agent authorization is temporarily unavailable") from error
        if resolved is None:
            return None
        return AccessToken(token=token, client_id=resolved.credential_id, scopes=[], expires_at=None, claims={})

    async def resolve_static_actor(self, access_token: AccessToken) -> AgentActor | None:
        resolved = await self._authority.resolve(access_token.token, record_seen=True)
        if resolved is None or resolved.credential_id != access_token.client_id:
            return None
        return resolved.actor


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
    agent_bearer_authority: AgentBearerAuthority | None = None,
) -> McpAuth:
    """Compose FastMCP protocol auth with Haku's canonical Agent authority.

    FastMCP owns DCR, PKCE, callback, client, and token-family storage. Haku's OAuth adapter
    delegates every product authorization decision to ``agent_authority``. Static credentials use
    the same authority and are accepted only when their exact fingerprint-backed binding is active.
    """
    ensure_supported_fastmcp_version()
    agent_bearer_authority = agent_bearer_authority or build_agent_bearer_authority(
        agent_authority=agent_authority, static_credentials=static_credentials, session_tokens=session_tokens
    )
    agent_bearer_verifier = _AgentBearerTokenVerifier(agent_bearer_authority)
    has_bearer = agent_bearer_authority.configured
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
                verifiers=[agent_bearer_verifier],
                operator_session_authenticator=operator_session_authenticator,
            ),
            storage=storage,
            static_actor_resolver=agent_bearer_verifier if has_bearer else None,
        )
    if not has_bearer:
        raise ValueError(
            "haku-console /mcp has no configured credential: set at least one static Agent "
            "(config_file `static_agents`) or `mcp_oauth`"
        )
    return StaticMcpAuth(
        provider=HakuFailurePreservingMultiAuth(
            server=agent_bearer_verifier, verifiers=[], operator_session_authenticator=operator_session_authenticator
        ),
        static_actor_resolver=agent_bearer_verifier,
    )
