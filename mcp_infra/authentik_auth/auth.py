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
   RFC 7521 jwt-bearer client_credentials. Tokens are cached in-memory and
   optionally persisted to an ``AsyncKeyValue`` store (same interface FastMCP's
   OIDCProxy uses for its state). Uses a long-lived ``AsyncOAuth2Client`` for
   the exchange calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2 import OAuth2Error
from authlib.oauth2.rfc6749.wrappers import OAuth2Token
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AuthProvider, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token, get_http_headers
from glide_shared.exceptions import TimeoutError as GlideTimeoutError
from key_value.aio.protocols import AsyncKeyValue
from mcp.server.auth.provider import TokenError
from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mcp.server.auth.provider import RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

# Scopes requested when exchanging the caller's token for a proxy-scoped one.
# Each is load-bearing — see x/authentik_mcp_poc/NOTES.md §6 for why `ak_proxy`
# is required (without it the outpost forwards empty identity headers).
EXCHANGE_SCOPES = "openid email profile ak_proxy"

# Safety margin (seconds) for token expiry checks — authlib's
# OAuth2Token.is_expired(leeway=N) subtracts this from expires_at.
_EXPIRY_LEEWAY = 30

# Scopes that OIDCProxy's DCR endpoint will accept from MCP clients.
# These must also be configured as property_mappings on the Authentik OAuth2
# provider (Authentik silently drops scopes without a matching ScopeMapping).
# - offline_access: triggers Authentik to issue a refresh token, so claude.ai
#   can silently renew sessions without re-authenticating.
DEFAULT_VALID_SCOPES = ["openid", "email", "profile", "offline_access"]


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
    extra_jwt_issuers: tuple[str, ...] = Field(
        default=(),
        description="Additional issuers the JWTVerifier accepts (beyond oidc_issuer). "
        "Only valid for providers that share oidc_issuer's signing key, so the same "
        "JWKS validates their tokens. Used for dedicated machine client_credentials "
        "providers reaching the same MCP.",
    )

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


# ── Resilient refresh proxy ───────────────────────────────────────────────

UPSTREAM_REFRESH_FAILURES = Counter(
    "mcp_auth_upstream_refresh_failures_total",
    "MCP client token refreshes that failed while talking to Authentik or persisting OAuth state",
    ["outcome"],  # transient (upstream unreachable/5xx) | oauth (real OAuth error response) | storage
)

_RETRY_AFTER_SECONDS = 60


def _transient_upstream_error(exc: BaseException | None) -> bool:
    """True for upstream failures that say nothing about the grant's validity."""
    if isinstance(exc, httpx.TransportError):  # DNS, connect, timeout, protocol errors
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def _is_transient_token_error(exc: BaseException) -> bool:
    return isinstance(exc, TokenError) and _transient_upstream_error(exc.__cause__)


def _transient_oauth_state_storage_error(exc: BaseException) -> bool:
    """True for local OAuth-state store failures that should be retryable."""
    return isinstance(exc, GlideTimeoutError)


def _transient_refresh_error(exc: BaseException) -> bool:
    return _is_transient_token_error(exc) or _transient_oauth_state_storage_error(exc)


def _upstream_oauth_rejection(exc: BaseException | None) -> bool:
    """True when Authentik itself answered the refresh with an OAuth rejection.

    authlib raises OAuth2Error subclasses for error-body responses; a
    non-5xx HTTPStatusError is an upstream rejection without a parseable
    body. TokenErrors with any other cause (or none) are local — unknown
    refresh token, missing JTI mapping — i.e. normal client churn that never
    reached Authentik, and must not fire the upstream-failure alert.
    """
    return isinstance(exc, OAuth2Error | httpx.HTTPStatusError)


class ResilientOIDCProxy(OIDCProxy):
    """OIDCProxy that doesn't convert transient upstream failures into invalid_grant.

    FastMCP's `OAuthProxy.exchange_refresh_token` wraps the upstream refresh call
    in a blanket `except Exception` and raises
    `TokenError("invalid_grant")`. Per RFC 6749 §5.2, invalid_grant tells the client
    its refresh token is revoked — claude.ai correctly treats it as terminal, flips
    the connector to "Reconnect", and never retries. A single Authentik restart
    (503) or DNS blip therefore permanently killed connectors
    (see debug/2026_06_claude_ai_connector_deauth.md).

    This subclass retries transient upstream failures briefly, and if they persist
    answers HTTP 503 + Retry-After (via Starlette's default HTTPException handler)
    instead of invalid_grant. Genuine upstream OAuth error responses (Authentik
    actually rejecting the grant) still surface as invalid_grant.

    Detection relies on FastMCP raising its TokenError `from` the original httpx
    exception — pinned by test_auth.py against the locked FastMCP version.
    """

    # Short on purpose: retries only bridge sub-second blips; longer outages are
    # answered with 503 so the client may retry. Class attrs so tests can drop
    # the wait.
    refresh_retry_stop = stop_after_attempt(3)
    refresh_retry_wait = wait_exponential(multiplier=0.5, max=2)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_transient_refresh_error),
                stop=self.refresh_retry_stop,
                wait=self.refresh_retry_wait,
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    return await super().exchange_refresh_token(client, refresh_token, scopes)
        except TokenError as e:
            if _is_transient_token_error(e):
                UPSTREAM_REFRESH_FAILURES.labels(outcome="transient").inc()
                logger.exception("Upstream auth server unavailable during token refresh")
                raise HTTPException(
                    status_code=503,
                    detail="Upstream authorization server temporarily unavailable; retry later.",
                    headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
                ) from e.__cause__
            if _upstream_oauth_rejection(e.__cause__):
                UPSTREAM_REFRESH_FAILURES.labels(outcome="oauth").inc()
            raise
        except Exception as e:
            if _transient_oauth_state_storage_error(e):
                UPSTREAM_REFRESH_FAILURES.labels(outcome="storage").inc()
                logger.exception("OAuth state store unavailable during token refresh")
                raise HTTPException(
                    status_code=503,
                    detail="OAuth state store temporarily unavailable; retry later.",
                    headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
                ) from e
            raise
        raise AssertionError("unreachable")  # the retry loop always returns or raises


# ── Auth builder ──────────────────────────────────────────────────────────


def build_authentik_auth(
    config: AuthentikAuthConfig,
    *,
    valid_scopes: list[str] | None = None,
    client_storage: Any | None = None,
    extra_verifiers: list[TokenVerifier] | None = None,
) -> AuthProvider:
    """Build OIDCProxy + JWTVerifier auth for an Authentik-backed MCP server.

    OIDCProxy handles the user-facing MCP OAuth dance (DCR, PKCE, consent).
    JWTVerifier validates tool-call Bearer tokens against Authentik's JWKS,
    whose URL is taken from the OIDC discovery document (Authentik serves
    JWKS at ``<issuer>/jwks/``, not ``<issuer>/.well-known/jwks``).

    Args:
        config: Authentik auth configuration.
        valid_scopes: Scopes OIDCProxy's DCR endpoint will accept. Defaults
            to ``DEFAULT_VALID_SCOPES``.
        client_storage: Optional ``AsyncKeyValue`` backend for OIDCProxy state
            (DCR registrations, tokens). Defaults to FastMCP's file-based
            encrypted store under ``FASTMCP_HOME``.
        extra_verifiers: Additional ``TokenVerifier``s appended to the MultiAuth
            after the Authentik JWTVerifier — e.g. a ``StaticTokenVerifier`` so a
            machine caller's fixed bearer is accepted on the same endpoint as the
            human OAuth flow. Each is tried in turn; the first to accept wins.
    """
    issuer = config.normalized_issuer()
    config_url = f"{issuer}/.well-known/openid-configuration"
    discovery = httpx.get(config_url, timeout=10.0).raise_for_status().json()
    jwks_uri = discovery["jwks_uri"]
    proxy = ResilientOIDCProxy(
        config_url=config_url,
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret,
        base_url=config.normalized_public_base_url(),
        require_authorization_consent=True,
        client_storage=client_storage,
    )
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = valid_scopes or DEFAULT_VALID_SCOPES
    # Accept each issuer both with and without a trailing slash. `normalized_issuer()`
    # strips the slash, but Authentik's per-provider tokens carry `iss` WITH a
    # trailing slash and JWTVerifier compares `iss` to the configured issuer by exact
    # string match — so the bare form alone rejects every real Authentik token. (Not
    # caught before because claude.ai authenticates through OIDCProxy, never the
    # JWTVerifier path; direct machine bearer tokens do.)
    #
    # extra_jwt_issuers lets the verifier also accept tokens from sibling providers
    # that share this provider's signing key (so the same JWKS validates them) — e.g.
    # a dedicated machine client_credentials provider whose tokens reach the same MCP.
    issuers = [issuer, issuer + "/"]
    for extra in config.extra_jwt_issuers:
        bare = extra.rstrip("/")
        issuers += [bare, bare + "/"]
    verifiers: list[TokenVerifier] = [JWTVerifier(jwks_uri=jwks_uri, issuer=issuers)]
    if extra_verifiers:
        verifiers.extend(extra_verifiers)
    return MultiAuth(server=proxy, verifiers=verifiers)


# ── Token exchange auth ───────────────────────────────────────────────────

_EXCHANGE_TOKEN_COLLECTION = "mcp-exchange-tokens"


def _cache_key(upstream_token: str) -> str:
    """Derive a stable, collision-free cache key from an upstream JWT."""
    return hashlib.sha256(upstream_token.encode()).hexdigest()


def _token_expired(token_data: dict[str, Any]) -> bool:
    """Check if a token dict has expired (with leeway)."""
    expires_at = token_data.get("expires_at")
    if expires_at is None:
        return True
    return time.time() >= float(expires_at) - _EXPIRY_LEEWAY


def _upstream_access_token() -> str:
    """Return the bearer credential from the originating MCP request."""
    authorization = get_http_headers(include={"authorization"}).get("authorization")
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token:
            raise RuntimeError("authenticated MCP request has a malformed Authorization header")
        return token
    access = get_access_token()
    if access is None:
        raise RuntimeError("no authenticated access token in request context")
    return access.token


class AuthentikExchangeAuth(httpx.Auth):
    """httpx Auth that mints a proxy-provider-scoped JWT per request.

    Wraps a long-lived `AsyncOAuth2Client` for making token exchange calls
    to Authentik. Exchanged tokens are cached in-memory and optionally
    persisted to an ``AsyncKeyValue`` store for survival across pod restarts.

    Call `aclose()` to release the underlying HTTP client when done.
    """

    def __init__(self, config: AuthentikAuthConfig, *, token_store: AsyncKeyValue | None = None) -> None:
        if config.proxy_client_id is None:
            raise ValueError("proxy_client_id is required for AuthentikExchangeAuth")
        self._config = config
        self._exchange_client = AsyncOAuth2Client(client_id=config.proxy_client_id, timeout=config.exchange_timeout)
        self._token_store = token_store
        # In-memory cache: upstream JWT hash → OAuth2Token.
        self._cache: dict[str, OAuth2Token] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the underlying exchange client."""
        await self._exchange_client.aclose()

    async def _load_from_store(self, key: str) -> OAuth2Token | None:
        """Try to load a non-expired token from the persistent store."""
        if self._token_store is None:
            return None
        stored = await self._token_store.get(key, collection=_EXCHANGE_TOKEN_COLLECTION)
        if stored is None or _token_expired(stored):
            return None
        return OAuth2Token(stored)

    async def _save_to_store(self, key: str, token_data: OAuth2Token) -> None:
        """Persist a token to the store with TTL matching its lifetime."""
        if self._token_store is None:
            return
        expires_in = token_data.get("expires_in")
        ttl = max(float(expires_in) - _EXPIRY_LEEWAY, 0) if expires_in is not None else None
        await self._token_store.put(key, dict(token_data), collection=_EXCHANGE_TOKEN_COLLECTION, ttl=ttl)

    async def _get_exchanged_token(self, upstream_token: str) -> str:
        """Return a cached or freshly exchanged proxy-scoped token."""
        key = _cache_key(upstream_token)

        # Fast path: in-memory cache (no lock).
        cached = self._cache.get(key)
        if cached is not None and not cached.is_expired(leeway=_EXPIRY_LEEWAY):
            return str(cached["access_token"])

        async with self._lock:
            # Re-check in-memory after acquiring lock.
            cached = self._cache.get(key)
            if cached is not None and not cached.is_expired(leeway=_EXPIRY_LEEWAY):
                return str(cached["access_token"])

            # Check persistent store.
            restored = await self._load_from_store(key)
            if restored is not None:
                self._cache[key] = restored
                logger.debug("restored exchanged token from store (expires_at=%s)", restored.get("expires_at"))
                return str(restored["access_token"])

            # Cache miss everywhere — exchange.
            token_data: OAuth2Token = await self._exchange_client.fetch_token(
                url=self._config.authentik_token_endpoint(),
                grant_type="client_credentials",
                client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                client_assertion=upstream_token,
                scope=EXCHANGE_SCOPES,
            )
            self._cache[key] = token_data
            await self._save_to_store(key, token_data)
            logger.debug(
                "exchanged and cached token (expires_in=%s, expires_at=%s)",
                token_data.get("expires_in"),
                token_data.get("expires_at"),
            )
            return str(token_data["access_token"])

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._get_exchanged_token(_upstream_access_token())
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
