"""Shared auth wiring for Authentik-backed MCP servers.

Three components:

1. `AuthentikAuthConfig` — frozen Pydantic model capturing the auth-only
   fields needed to wire OIDCProxy + JWTVerifier and perform JWT-bearer
   token exchanges against an Authentik proxy provider outpost. Because
   it's a Pydantic model, it doubles as a `BaseSettings` nested field so
   downstream servers can load auth from env vars without keeping a
   parallel `*Settings` twin.

2. `build_authentik_auth` — constructs the FastMCP AuthProvider (OIDCProxy +
   JWTVerifier + MultiAuth) that handles the MCP OAuth dance with claude.ai.

3. `AuthentikExchangeAuth` — an httpx.Auth subclass that transparently exchanges
   the MCP user's upstream Authentik JWT for a proxy-provider-scoped JWT via
   RFC 7521 jwt-bearer client_credentials. Tokens are cached per upstream JWT
   using authlib's `OAuth2Token` for expiry tracking. Uses a long-lived
   `AsyncOAuth2Client` for the exchange calls (consistent with FastMCP's own
   auth internals).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749.wrappers import OAuth2Token
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# Scopes requested when exchanging the caller's token for a proxy-scoped one.
# Each is load-bearing — see x/authentik_mcp_poc/NOTES.md §6 for why `ak_proxy`
# is required (without it the outpost forwards empty identity headers).
EXCHANGE_SCOPES = "openid email profile ak_proxy"

# Safety margin (seconds) for token expiry checks — authlib's
# OAuth2Token.is_expired(leeway=N) subtracts this from expires_at.
_EXPIRY_LEEWAY = 30

DEFAULT_VALID_SCOPES = ["openid", "email", "profile"]


# ── Config ────────────────────────────────────────────────────────────────


class AuthentikAuthConfig(BaseModel):
    """Auth-only config for an Authentik-backed MCP server.

    Core fields (oidc_issuer through public_base_url) are needed by
    `build_authentik_auth`. Exchange fields (proxy_client_id, exchange_timeout)
    are only needed when using `AuthentikExchangeAuth` for JWT-bearer token
    exchange against a proxy provider outpost.
    """

    model_config = ConfigDict(frozen=True)

    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    public_base_url: str
    proxy_client_id: str | None = None
    exchange_timeout: float = 10.0

    def normalized_public_base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    def normalized_issuer(self) -> str:
        return self.oidc_issuer.rstrip("/")

    def authentik_token_endpoint(self) -> str:
        """Global Authentik `/application/o/token/` URL derived from `oidc_issuer`.

        Strips the trailing provider slug, preserving any reverse-proxy path
        prefix before `/application/o/`.
        """
        parsed = urlparse(self.oidc_issuer.rstrip("/"))
        prefix, marker, provider_slug = parsed.path.rpartition("/application/o/")
        if not marker or not provider_slug or "/" in provider_slug:
            raise ValueError(
                "oidc_issuer must end in an Authentik per-provider issuer path "
                f"like `.../application/o/<slug>/`; got {self.oidc_issuer!r}"
            )
        return urlunparse(parsed._replace(path=f"{prefix}{marker}token/"))


# ── Auth builder ──────────────────────────────────────────────────────────


def build_authentik_auth(
    config: AuthentikAuthConfig, *, valid_scopes: list[str] | None = None, client_storage: Any | None = None
) -> AuthProvider:
    """Build OIDCProxy + JWTVerifier auth for an Authentik-backed MCP server.

    OIDCProxy handles the user-facing MCP OAuth dance (DCR, PKCE, consent).
    JWTVerifier validates tool-call Bearer tokens against Authentik's JWKS.

    Args:
        config: Authentik auth configuration.
        valid_scopes: Scopes OIDCProxy's DCR endpoint will accept. Defaults
            to ``["openid", "email", "profile"]``.
        client_storage: Optional ``AsyncKeyValue`` backend for OIDCProxy state
            (DCR registrations, tokens). Defaults to FastMCP's file-based
            encrypted store under ``FASTMCP_HOME``.
    """
    issuer = config.normalized_issuer()
    proxy = OIDCProxy(
        config_url=f"{issuer}/.well-known/openid-configuration",
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret,
        base_url=config.normalized_public_base_url(),
        require_authorization_consent=True,
        client_storage=client_storage,
    )
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = valid_scopes or DEFAULT_VALID_SCOPES
    return MultiAuth(server=proxy, verifiers=[JWTVerifier(jwks_uri=f"{issuer}/.well-known/jwks", issuer=issuer)])


# ── Token exchange auth ───────────────────────────────────────────────────


class AuthentikExchangeAuth(httpx.Auth):
    """httpx Auth that mints a proxy-provider-scoped JWT per request.

    Wraps a long-lived `AsyncOAuth2Client` for making token exchange calls
    to Authentik. Exchanged tokens are cached per upstream user JWT using
    authlib's `OAuth2Token` for expiry tracking.

    Call `aclose()` to release the underlying HTTP client when done.
    """

    def __init__(self, config: AuthentikAuthConfig) -> None:
        if config.proxy_client_id is None:
            raise ValueError("proxy_client_id is required for AuthentikExchangeAuth")
        self._config = config
        self._exchange_client = AsyncOAuth2Client(client_id=config.proxy_client_id, timeout=config.exchange_timeout)
        # Per-user cache: upstream JWT → OAuth2Token (with expires_at tracking).
        self._cache: dict[str, OAuth2Token] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the underlying exchange client."""
        await self._exchange_client.aclose()

    async def _get_exchanged_token(self, upstream_token: str) -> str:
        """Return a cached or freshly exchanged proxy-scoped token."""
        # Fast path: check cache without lock.
        cached = self._cache.get(upstream_token)
        if cached is not None and not cached.is_expired(leeway=_EXPIRY_LEEWAY):
            return str(cached["access_token"])

        # Slow path: acquire lock, re-check, then exchange.
        async with self._lock:
            cached = self._cache.get(upstream_token)
            if cached is not None and not cached.is_expired(leeway=_EXPIRY_LEEWAY):
                return str(cached["access_token"])

            token_data: OAuth2Token = await self._exchange_client.fetch_token(
                url=self._config.authentik_token_endpoint(),
                grant_type="client_credentials",
                client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                client_assertion=upstream_token,
                scope=EXCHANGE_SCOPES,
            )
            self._cache[upstream_token] = token_data
            logger.debug(
                "cached exchanged token (expires_in=%s, expires_at=%s)",
                token_data.get("expires_in"),
                token_data.get("expires_at"),
            )
            return str(token_data["access_token"])

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        access = get_access_token()
        if access is None:
            raise RuntimeError("no authenticated access token in request context")
        token = await self._get_exchanged_token(access.token)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
