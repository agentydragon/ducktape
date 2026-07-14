"""Authentication composition for Haku's agent-facing MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mcp.server.auth.provider import TokenError
from starlette.exceptions import HTTPException

from haku.console.config import Settings
from haku.console.mcp_config import ResolvedStaticAgent, static_agent_client_id
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
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
    from mcp.server.auth.provider import AuthorizationCode
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

_RETRY_AFTER_SECONDS = 60
_INVALID_GRANT_DESCRIPTION = "Authorization grant identity is invalid."


def _static_bearer_verifier(static_agents: list[ResolvedStaticAgent]) -> StaticTokenVerifier | None:
    """Build the fixed-bearer verifier for configured machine agents."""
    if not static_agents:
        return None
    return StaticTokenVerifier(
        tokens={
            agent.token.get_secret_value(): {"client_id": static_agent_client_id(agent.agent), "scopes": []}
            for agent in static_agents
        }
    )


@dataclass(frozen=True)
class StaticMcpAuth:
    provider: AuthProvider


@dataclass(frozen=True)
class OAuthMcpAuth:
    provider: AuthProvider
    storage: OAuthClientStorage


type McpAuth = StaticMcpAuth | OAuthMcpAuth


class _AgentOperatorLinkRejectedError(Exception):
    pass


@dataclass(frozen=True)
class _LinkAgentOperator:
    store: PostgresMcpOperatorOAuthStore

    async def __call__(self, client_id: str, principal: VerifiedOidcPrincipal) -> None:
        try:
            self.store.bind_agent_operator(agent_dcr_client_id=client_id, operator_subject=principal.subject)
        except ValueError as error:
            raise _AgentOperatorLinkRejectedError from error


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
        on_client_authorized: _LinkAgentOperator,
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
        self._on_client_authorized = on_client_authorized

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
            await self._on_client_authorized(client.client_id, principal)
        except _AgentOperatorLinkRejectedError:
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError("invalid_grant", _INVALID_GRANT_DESCRIPTION) from None
        return await super().exchange_authorization_code(client, authorization_code)


def build_auth(
    settings: Settings, static_agents: list[ResolvedStaticAgent], *, operator_oauth_store: PostgresMcpOperatorOAuthStore
) -> McpAuth:
    """Compose the credentials accepted by Haku's agent-facing MCP server.

    OAuth mode combines FastMCP's Authentik-backed authorization server with the configured
    static-agent bearers. Its shared storage and DCR-client-to-Operator ownership callback remain
    inseparable from the provider. Static-only mode accepts the configured fixed bearers. Running
    with neither credential type is a configuration error.
    """
    static = _static_bearer_verifier(static_agents)
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
            on_client_authorized=_LinkAgentOperator(operator_oauth_store),
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
