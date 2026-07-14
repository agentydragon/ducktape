"""FastMCP provider composition for Authentik-backed MCP servers."""

from collections.abc import Collection
from typing import Any

from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from mcp_infra.authentik_auth.config import AuthentikAuthConfig, DirectJwtTrust
from mcp_infra.authentik_auth.fastmcp_proxy import DownstreamClientIdentityOIDCProxy

# Scopes that OIDCProxy's DCR endpoint will accept from MCP clients.
# These must also be configured as property_mappings on the Authentik OAuth2
# provider (Authentik silently drops scopes without a matching ScopeMapping).
# - offline_access: triggers Authentik to issue a refresh token, so claude.ai
#   can silently renew sessions without re-authenticating.
DEFAULT_VALID_SCOPES = ["openid", "email", "profile", "offline_access"]


def compose_authentik_auth(
    *,
    proxy: DownstreamClientIdentityOIDCProxy,
    direct_jwt_trusts: Collection[DirectJwtTrust] = (),
    extra_verifiers: list[TokenVerifier] | None = None,
) -> AuthProvider:
    """Layer explicit bearer-token verifiers around an already-built proxy.

    The caller remains the sole owner of proxy construction, storage, scopes,
    and any protocol checkpoint behavior. This function only composes the
    requested direct-JWT and extra verifier paths with that proxy.
    """
    verifiers: list[TokenVerifier] = []
    for trust in direct_jwt_trusts:
        bare_issuer = trust.issuer.rstrip("/")
        verifiers.append(
            JWTVerifier(
                jwks_uri=str(proxy.oidc_config.jwks_uri),
                issuer=[bare_issuer, bare_issuer + "/"],
                audience=list(trust.audiences),
                required_scopes=list(trust.required_scopes) or None,
            )
        )
    if extra_verifiers:
        verifiers.extend(extra_verifiers)
    return MultiAuth(server=proxy, verifiers=verifiers)


def build_authentik_auth(
    config: AuthentikAuthConfig,
    *,
    valid_scopes: list[str] | None = None,
    client_storage: Any | None = None,
    extra_verifiers: list[TokenVerifier] | None = None,
) -> AuthProvider:
    """Build OIDCProxy plus explicit direct-JWT trust for an Authentik-backed MCP server.

    OIDCProxy handles the user-facing MCP OAuth dance (DCR, PKCE, consent).
    Configured direct-JWT trusts validate machine Bearer tokens against
    Authentik's discovery-advertised JWKS, audience, and required scopes.

    Args:
        config: Authentik auth configuration.
        valid_scopes: Scopes OIDCProxy's DCR endpoint will accept. Defaults
            to ``DEFAULT_VALID_SCOPES``.
        client_storage: Optional ``AsyncKeyValue`` backend for OIDCProxy state
            (DCR registrations, tokens). Defaults to FastMCP's file-based
            encrypted store under ``FASTMCP_HOME``.
        extra_verifiers: Additional ``TokenVerifier``s appended to the MultiAuth
            after configured direct-JWT verifiers — e.g. a ``StaticTokenVerifier`` so a
            machine caller's fixed bearer is accepted on the same endpoint as the
            human OAuth flow. Each is tried in turn; the first to accept wins.
    """
    issuer = config.normalized_issuer()
    config_url = f"{issuer}/.well-known/openid-configuration"
    proxy = DownstreamClientIdentityOIDCProxy(
        config_url=config_url,
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret,
        base_url=config.normalized_public_base_url(),
        require_authorization_consent=True,
        client_storage=client_storage,
    )
    proxy.update_default_scopes(valid_scopes or DEFAULT_VALID_SCOPES)
    return compose_authentik_auth(
        proxy=proxy, direct_jwt_trusts=config.direct_jwt_trusts, extra_verifiers=extra_verifiers
    )
