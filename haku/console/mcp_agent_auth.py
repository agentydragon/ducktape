"""Authentication composition for Haku's Agent-facing MCP server."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastmcp.server.auth.auth import AccessToken, AuthProvider, TokenVerifier

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentAuthorization,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.config import Settings
from haku.console.mcp_auth.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    BearerVerificationUnavailableError,
    HakuAgentOAuthProxy,
    HakuFailurePreservingMultiAuth,
    StaticAgentActorResolver,
    assert_fastmcp_adapter_compatibility,
)
from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES
from mcp_infra.persistence import OAuthClientStorage, build_shared_client_storage

_STATIC_BINDING_CLIENT_ID_PREFIX = "haku-static-binding:"


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
            agent_id=authorization.agent_id, operator_id=authorization.operator_id, binding_id=authorization.binding_id
        )

    async def _authorization(self, token: str, *, verification: bool) -> StaticAgentAuthorization | None:
        try:
            fingerprint = self._credentials.configured_fingerprint(token)
            if fingerprint is None:
                return None
            return await self._authority.static_authorization_for_fingerprint(fingerprint=fingerprint)
        except (StaticAgentRejectedError, ValueError):
            return None
        except AgentGrantAuthorityUnavailableError as error:
            if verification:
                raise BearerVerificationUnavailableError("Agent authorization is temporarily unavailable") from error
            raise


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


def build_auth(
    settings: Settings, *, agent_authority: PostgresAgentAuthority, static_credentials: StaticAgentCredentialRegistry
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
    if settings.mcp_oauth is not None:
        storage = build_shared_client_storage(settings.mcp_oauth.persistence)
        config = settings.mcp_oauth.as_authentik_auth_config(public_base_url=settings.public_base_url)
        proxy = HakuAgentOAuthProxy(
            config_url=f"{config.normalized_issuer()}/.well-known/openid-configuration",
            client_id=config.oidc_client_id,
            client_secret=config.oidc_client_secret,
            base_url=config.normalized_public_base_url(),
            client_storage=storage,
            expected_issuer=config.oidc_issuer,
            grant_authority=agent_authority,
        )
        proxy.update_default_scopes(DEFAULT_VALID_SCOPES)
        return OAuthMcpAuth(
            provider=HakuFailurePreservingMultiAuth(server=proxy, verifiers=[static] if static is not None else []),
            storage=storage,
            static_actor_resolver=static,
        )
    if static is None:
        raise ValueError(
            "haku-console /mcp has no configured credential: set at least one static Agent "
            "(config_file `static_agents`) or `mcp_oauth`"
        )
    return StaticMcpAuth(
        provider=HakuFailurePreservingMultiAuth(server=static, verifiers=[]), static_actor_resolver=static
    )
