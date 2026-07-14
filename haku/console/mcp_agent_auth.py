"""Authentication composition for Haku's agent-facing MCP server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastmcp.server.auth.auth import AccessToken, AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mcp.server.auth.provider import TokenError
from starlette.exceptions import HTTPException

from haku.console.config import Settings
from haku.console.mcp_config import ResolvedStaticAgent, static_agent_client_id, static_agent_name_from_client_id
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from haku.console.operator_identity import OperatorIdentityError, VerifiedExternalIdentity
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.fastmcp_proxy import DownstreamClientIdentityOIDCProxy
from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)
from mcp_infra.authentik_auth.provider import DEFAULT_VALID_SCOPES, compose_authentik_auth
from mcp_infra.persistence import OAuthClientStorage, build_shared_client_storage

if TYPE_CHECKING:
    from mcp.server.auth.provider import AuthorizationCode, RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

_RETRY_AFTER_SECONDS = 60
_INVALID_GRANT_DESCRIPTION = "Authorization grant identity is invalid."


class _ActiveOperatorStaticTokenVerifier(StaticTokenVerifier):
    """Static bearer verification with a live canonical Operator status check."""

    def __init__(self, static_agents: list[ResolvedStaticAgent], identity_store: PostgresOperatorIdentityStore) -> None:
        super().__init__(
            tokens={
                agent.token.get_secret_value(): {"client_id": static_agent_client_id(agent.agent), "scopes": []}
                for agent in static_agents
            }
        )
        self._operators_by_client_id = {
            static_agent_client_id(agent.agent): agent.operator_id for agent in static_agents
        }
        self._identity_store = identity_store

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await super().verify_token(token)
        if access_token is None or not access_token.client_id:
            return None
        operator_id = self._operators_by_client_id.get(access_token.client_id)
        if operator_id is None or not self._identity_store.is_active(operator_id):
            return None
        return access_token


def _static_bearer_verifier(
    static_agents: list[ResolvedStaticAgent], identity_store: PostgresOperatorIdentityStore
) -> StaticTokenVerifier | None:
    """Build the fixed-bearer verifier for configured machine agents."""
    if not static_agents:
        return None
    return _ActiveOperatorStaticTokenVerifier(static_agents, identity_store)


@dataclass(frozen=True)
class StaticMcpAuth:
    provider: AuthProvider


@dataclass(frozen=True)
class OAuthMcpAuth:
    provider: AuthProvider
    storage: OAuthClientStorage


type McpAuth = StaticMcpAuth | OAuthMcpAuth


def resolve_mcp_agent(
    client_id: str,
    static_agents: list[ResolvedStaticAgent],
    oauth_store: PostgresMcpOperatorOAuthStore,
    identity_store: PostgresOperatorIdentityStore,
) -> AgentActor | None:
    """Resolve one authenticated MCP client id to its active canonical Operator."""
    static_name = static_agent_name_from_client_id(client_id)
    if static_name is not None:
        agent = next((agent for agent in static_agents if agent.agent == static_name), None)
        if agent is None or not identity_store.is_active(agent.operator_id):
            return None
        return AgentActor(principal=agent.agent, operator_id=agent.operator_id)

    operator_id = oauth_store.agent_operator(client_id)
    if operator_id is None or not identity_store.is_active(operator_id):
        return None
    return AgentActor(principal=client_id, operator_id=operator_id)


class _AgentOperatorLinkRejectedError(Exception):
    pass


@dataclass(frozen=True)
class _AgentOperatorLinkAuthority:
    """Canonical ownership service shared by OAuth issuance and token authentication."""

    store: PostgresMcpOperatorOAuthStore
    identity_store: PostgresOperatorIdentityStore

    async def link_verified_client(self, client_id: str, principal: VerifiedOidcPrincipal) -> None:
        try:
            identity = self.identity_store.resolve_verified_identity(
                VerifiedExternalIdentity(issuer=principal.issuer, subject=principal.subject)
            )
            self.store.bind_agent_operator(agent_dcr_client_id=client_id, operator_id=identity.operator_id)
        except (OperatorIdentityError, ValueError) as error:
            raise _AgentOperatorLinkRejectedError from error

    def active_operator_for_client(self, client_id: str) -> UUID | None:
        return self.store.agent_operator(client_id)


class _VerifiedPrincipalOIDCProxy(DownstreamClientIdentityOIDCProxy):
    """Verify the upstream operator before FastMCP consumes the downstream code."""

    def __init__(
        self,
        *,
        config_url: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        client_storage: OAuthClientStorage,
        expected_issuer: str,
        client_link_authority: _AgentOperatorLinkAuthority,
    ) -> None:
        super().__init__(
            config_url=config_url,
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            require_authorization_consent=True,
            client_storage=client_storage,
        )
        self._principal_resolver = AuthentikOidcPrincipalResolver(
            expected_issuer=expected_issuer,
            discovered_issuer=str(self.oidc_config.issuer) if self.oidc_config.issuer is not None else None,
            jwks_uri=str(self.oidc_config.jwks_uri) if self.oidc_config.jwks_uri is not None else None,
            signing_algorithms=self.oidc_config.id_token_signing_alg_values_supported,
            client_id=client_id,
        )
        self._client_link_authority = client_link_authority

    async def _active_operator_for_client(self, client_id: str) -> UUID | None:
        return await asyncio.to_thread(self._client_link_authority.active_operator_for_client, client_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Reject OAuth tokens whose restored DCR client has no active Operator binding.

        Verify the signed local reference only far enough to identify its downstream client before
        FastMCP can transparently refresh upstream credentials. Rechecking after FastMCP returns
        closes a concurrent disable/unlink during its validation or refresh I/O. Tool dependencies
        repeat this check when constructing the caller, but enforcing it here also protects MCP
        protocol methods and future tools before their implementation can forget the tenant gate.
        """
        try:
            downstream_client_id = self.jwt_issuer.verify_token(token).get("client_id")
        except Exception:
            # This provider owns only its signed FastMCP reference JWTs. Returning no match lets
            # MultiAuth try the independently configured static-bearer verifier.
            return None
        if not isinstance(downstream_client_id, str) or not downstream_client_id:
            return None
        if await self._active_operator_for_client(downstream_client_id) is None:
            return None
        access_token = await super().load_access_token(token)
        if access_token is None or access_token.client_id != downstream_client_id:
            return None
        if await self._active_operator_for_client(downstream_client_id) is None:
            return None
        return access_token

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # FastMCP exposes no public checkpoint between the upstream callback and
        # consuming this code, so Haku verifies the stored IdP response first.
        code_model = await self._code_store.get(key=authorization_code.code)
        if (
            code_model is None
            or not client.client_id
            or code_model.client_id != client.client_id
            or authorization_code.client_id != client.client_id
        ):
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION)
        try:
            principal = await self._principal_resolver.resolve(code_model.idp_tokens)
        except InvalidOidcPrincipalError:
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION) from None
        except OidcPrincipalVerificationUnavailableError:
            raise HTTPException(
                status_code=503,
                detail="Upstream identity verification temporarily unavailable; retry later.",
                headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
            ) from None
        try:
            await self._client_link_authority.link_verified_client(client.client_id, principal)
        except _AgentOperatorLinkRejectedError:
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION) from None
        if await self._active_operator_for_client(client.client_id) is None:
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION)
        token = await super().exchange_authorization_code(client, authorization_code)
        # FastMCP may already have persisted a family if disable wins this race. Never return it;
        # access and refresh entrypoints keep the inaccessible family rejected thereafter.
        if await self._active_operator_for_client(client.client_id) is None:
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION)
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate only a token family that remains linked to an active Operator.

        The second check closes a disable/unlink race while FastMCP calls the upstream provider.
        FastMCP may already have persisted an inaccessible rotation when that race loses, but Haku
        never returns it and every later access/refresh path continues to reject the family.
        """
        client_id = client.client_id
        if not client_id or refresh_token.client_id != client_id:
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION)
        if await self._active_operator_for_client(client_id) is None:
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION)
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        if await self._active_operator_for_client(client_id) is None:
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION)
        return token


def build_auth(
    settings: Settings,
    static_agents: list[ResolvedStaticAgent],
    *,
    operator_oauth_store: PostgresMcpOperatorOAuthStore,
    operator_identity_store: PostgresOperatorIdentityStore,
) -> McpAuth:
    """Compose the credentials accepted by Haku's agent-facing MCP server.

    OAuth mode combines FastMCP's Authentik-backed authorization server with the configured
    static-agent bearers. Its shared storage and DCR-client-to-Operator ownership callback remain
    inseparable from the provider. Static-only mode accepts the configured fixed bearers. Running
    with neither credential type is a configuration error.
    """
    static = _static_bearer_verifier(static_agents, operator_identity_store)
    if settings.mcp_oauth is not None:
        storage = build_shared_client_storage(settings.mcp_oauth.persistence)
        config = settings.mcp_oauth.as_authentik_auth_config(public_base_url=settings.public_base_url)
        proxy = _VerifiedPrincipalOIDCProxy(
            config_url=f"{config.normalized_issuer()}/.well-known/openid-configuration",
            client_id=config.oidc_client_id,
            client_secret=config.oidc_client_secret,
            base_url=config.normalized_public_base_url(),
            client_storage=storage,
            expected_issuer=config.oidc_issuer,
            client_link_authority=_AgentOperatorLinkAuthority(operator_oauth_store, operator_identity_store),
        )
        proxy.update_default_scopes(DEFAULT_VALID_SCOPES)
        return OAuthMcpAuth(
            provider=compose_authentik_auth(
                proxy=proxy,
                direct_jwt_trusts=config.direct_jwt_trusts,
                extra_verifiers=[static] if static is not None else None,
            ),
            storage=storage,
        )
    if static is None:
        raise ValueError(
            "haku-console /mcp has no configured credential: set at least one static agent "
            "(config_file `static_agents`) or `mcp_oauth`"
        )
    return StaticMcpAuth(provider=static)
