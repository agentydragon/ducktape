"""Authentication composition for Haku's agent-facing MCP server."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from haku.console.config import Settings
from haku.console.mcp_config import ResolvedStaticAgent, static_agent_client_id
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from mcp_infra.authentik_auth.auth import build_authentik_auth
from mcp_infra.persistence import OAuthClientStorage, build_shared_client_storage


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


def operator_subject_from_idp_tokens(idp_tokens: Mapping[str, Any]) -> str | None:
    """Read the current operator subject from an Agent OAuth token response.

    This preserves the existing unsigned ID-token behavior until P1 replaces it with the verified
    principal resolver. Both console providers use Authentik's stable `user_id` subject mode.
    """
    id_token = idp_tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    claims = jwt.decode(id_token, options={"verify_signature": False, "verify_aud": False})
    subject = claims.get("sub")
    return subject if isinstance(subject, str) else None


@dataclass(frozen=True)
class _LinkAgentOperator:
    store: PostgresMcpOperatorOAuthStore

    async def __call__(self, client_id: str, idp_tokens: Mapping[str, Any]) -> None:
        subject = operator_subject_from_idp_tokens(idp_tokens)
        if subject is None:
            raise RuntimeError(f"MCP OAuth id_token for client {client_id} carried no `sub` claim")
        self.store.bind_agent_operator(agent_dcr_client_id=client_id, operator_subject=subject)


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
        return OAuthMcpAuth(
            provider=build_authentik_auth(
                settings.mcp_oauth.as_authentik_auth_config(public_base_url=settings.public_base_url),
                client_storage=storage,
                extra_verifiers=[static] if static is not None else None,
                on_client_authorized=_LinkAgentOperator(operator_oauth_store),
            ),
            storage=storage,
        )
    if static is None:
        raise ValueError(
            "haku-console /mcp has no configured credential: set at least one static agent "
            "(config_file `static_agents`) or `mcp_oauth`"
        )
    return StaticMcpAuth(provider=static)
