"""Shared auth wiring for Authentik-backed MCP servers.

Three components:

1. `AuthentikAuthConfig` — frozen Pydantic model capturing the auth-only
   fields needed to wire OIDCProxy + direct machine-token verifiers and perform JWT-bearer
   token exchanges against an Authentik proxy provider outpost. Because
   it's a Pydantic model, it doubles as a `BaseSettings` nested field so
   downstream servers can load auth from env vars without keeping a
   parallel `*Settings` twin.

2. `build_authentik_auth` — constructs the FastMCP AuthProvider (OIDCProxy +
   explicitly configured JWTVerifiers + MultiAuth) that handles the MCP OAuth dance.

3. `AuthentikTokenExchanger` — an explicit, stateless resolver that exchanges
   the MCP user's upstream Authentik JWT for a proxy-provider-scoped JWT via
   RFC 7521 jwt-bearer client_credentials. Callers use it from a request-scoped
   FastMCP dependency and pass the returned credential to the backend client.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2 import OAuth2Error
from fastmcp.dependencies import CurrentAccessToken
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from glide_shared.exceptions import TimeoutError as GlideTimeoutError
from mcp.server.auth.provider import TokenError
from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from mcp.server.auth.provider import AuthorizationCode, RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

# Scopes requested when exchanging the caller's token for a proxy-scoped one.
# Each is load-bearing — see x/authentik_mcp_poc/NOTES.md §6 for why `ak_proxy`
# is required (without it the outpost forwards empty identity headers).
EXCHANGE_SCOPES = "openid email profile ak_proxy"

# Scopes that OIDCProxy's DCR endpoint will accept from MCP clients.
# These must also be configured as property_mappings on the Authentik OAuth2
# provider (Authentik silently drops scopes without a matching ScopeMapping).
# - offline_access: triggers Authentik to issue a refresh token, so claude.ai
#   can silently renew sessions without re-authenticating.
DEFAULT_VALID_SCOPES = ["openid", "email", "profile", "offline_access"]

# Invoked during an OAuth client's authorization-code exchange, before the local FastMCP token is
# issued, with the client's ``client_id`` and raw upstream token response (``id_token`` etc.). The
# caller validates or links the identity; exceptions prevent local token issuance.
OnClientAuthorized = Callable[[str, Mapping[str, Any]], Awaitable[None]]
BackendTokenProvider = Callable[..., Awaitable[str]]


# ── Config ────────────────────────────────────────────────────────────────


class DirectJwtTrust(BaseModel):
    """One explicitly trusted direct bearer-token issuer contract."""

    model_config = ConfigDict(frozen=True)

    issuer: str = Field(description="Exact JWT issuer, accepted with or without its trailing slash.")
    audiences: tuple[str, ...] = Field(min_length=1, description="Allowed JWT audience claims for this issuer.")
    required_scopes: tuple[str, ...] = Field(
        default=(), description="OAuth scopes every direct token from this issuer must contain."
    )


class AuthentikAuthConfig(BaseModel):
    """Auth-only config for an Authentik-backed MCP server.

    Core fields (oidc_issuer through public_base_url) are needed by
    `build_authentik_auth`. Exchange fields (proxy_client_id, exchange_timeout)
    are only needed when using `AuthentikTokenExchanger` for JWT-bearer token
    exchange against a proxy provider outpost.
    """

    model_config = ConfigDict(frozen=True)

    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    public_base_url: str
    proxy_client_id: str | None = None
    exchange_timeout: float = 10.0
    direct_jwt_trusts: tuple[DirectJwtTrust, ...] = Field(
        default=(),
        description="Direct machine-token issuers accepted alongside OIDCProxy. Each must share "
        "oidc_issuer's signing key because its JWKS validates the token. Audience and scopes are "
        "checked within each entry rather than combined across issuers.",
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
BACKEND_TOKEN_EXCHANGE_FAILURES = Counter(
    "mcp_auth_backend_token_exchange_failures_total",
    "Backend token exchanges that failed before tool execution",
    ["outcome"],  # oauth | transport | upstream | response
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

    def __init__(self, *args: Any, on_client_authorized: OnClientAuthorized | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_client_authorized = on_client_authorized

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Validate/link the authorized upstream identity before issuing the FastMCP token.

        The raw upstream token response is read from the stored code before ``super()`` consumes it.
        Hook failures propagate before a local token is minted, so an immutable client-ownership
        check cannot fail after handing a cross-tenant credential to the caller.
        """
        if self._on_client_authorized is not None:
            code_model = await self._code_store.get(key=authorization_code.code)
            if code_model is None or not client.client_id:
                raise RuntimeError("authorized MCP client is missing its stored upstream identity")
            await self._on_client_authorized(client.client_id, code_model.idp_tokens)
        return await super().exchange_authorization_code(client, authorization_code)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Restore the DCR client identity FastMCP's token swap discards.

        ``OAuthProxy.load_access_token`` returns the *upstream* validation result, whose
        ``client_id`` is the proxy's own upstream client — so every OAuth agent collapses
        onto one identity (observed live 2026-07-13: agent→operator lookups keyed by DCR
        client_id failed with "agent haku-console-mcp has no linked operator subject").
        The FastMCP reference JWT carries the real DCR ``client_id`` claim; re-attach it
        so per-agent identity (operator links, audit principals) survives the swap.
        """
        upstream_validated = await super().load_access_token(token)
        if upstream_validated is None:
            return None
        validated = (
            upstream_validated
            if isinstance(upstream_validated, AccessToken)
            else AccessToken.model_validate(upstream_validated.model_dump())
        )
        try:
            dcr_client_id = self.jwt_issuer.verify_token(token).get("client_id")
        except Exception:
            # super() accepted the token, so an unverifiable JWT here means a non-JWT
            # verifier matched (e.g. a static bearer path) — keep its identity as-is.
            return validated
        if dcr_client_id and dcr_client_id != validated.client_id:
            return validated.model_copy(update={"client_id": dcr_client_id})
        return validated

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
    on_client_authorized: OnClientAuthorized | None = None,
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
        on_client_authorized: Optional hook fired during an OAuth client's authorization-code
            exchange, before the local FastMCP token is issued, with its ``client_id`` and raw
            upstream token response. Exceptions prevent local token issuance.
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
        on_client_authorized=on_client_authorized,
    )
    assert proxy.client_registration_options is not None
    proxy.client_registration_options.valid_scopes = valid_scopes or DEFAULT_VALID_SCOPES
    # OIDCProxy verifies its own wire tokens. Direct bearer tokens are a separate,
    # opt-in machine path: each trust gets its own verifier so accepted issuers and
    # audiences cannot accidentally form a cross-product. Authentik emits issuer
    # values with a trailing slash, while hand-built fixtures often omit it.
    verifiers: list[TokenVerifier] = []
    for trust in config.direct_jwt_trusts:
        bare_issuer = trust.issuer.rstrip("/")
        verifiers.append(
            JWTVerifier(
                jwks_uri=jwks_uri,
                issuer=[bare_issuer, bare_issuer + "/"],
                audience=list(trust.audiences),
                required_scopes=list(trust.required_scopes) or None,
            )
        )
    if extra_verifiers:
        verifiers.extend(extra_verifiers)
    return MultiAuth(server=proxy, verifiers=verifiers)


# ── Backend token exchange ─────────────────────────────────────────────────


class BackendTokenExchangeError(Exception):
    """Authentik did not return a usable backend credential."""


def _record_backend_token_exchange_failure(outcome: str, error: BaseException | None = None) -> None:
    BACKEND_TOKEN_EXCHANGE_FAILURES.labels(outcome=outcome).inc()
    logger.warning(
        "Backend token exchange failed: outcome=%s error_type=%s",
        outcome,
        type(error).__name__ if error is not None else "missing_access_token",
    )


def _transient_exchange_error(error: BaseException) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and (
        error.response.status_code == 429 or error.response.status_code >= 500
    )


def _raise_transient_token_status(response: httpx.Response) -> httpx.Response:
    """Make retryable statuses visible before Authlib parses OAuth JSON.

    Authlib raises for 5xx responses itself, but parses a 429 body directly into
    ``OAuthError``. Raising here preserves the HTTP status so the exchange retry
    policy can distinguish rate limiting from a terminal OAuth rejection.
    """
    if response.status_code == 429 or response.status_code >= 500:
        response.raise_for_status()
    return response


class AuthentikTokenExchanger:
    """Resolve one upstream identity token to one backend-scoped token.

    The exchanger deliberately owns no token cache or shared OAuth client.
    ``AsyncOAuth2Client.fetch_token`` mutates client-local token state, so each
    resolution creates and closes its own client. FastMCP's request-scoped
    dependency cache still ensures one resolution when multiple dependencies in
    the same tool invocation share the same provider.
    """

    exchange_retry_stop = stop_after_attempt(3)
    exchange_retry_wait = wait_exponential(multiplier=0.25, max=1)

    def __init__(self, config: AuthentikAuthConfig) -> None:
        if config.proxy_client_id is None:
            raise ValueError("proxy_client_id is required for AuthentikTokenExchanger")
        self._config = config

    async def exchange(self, upstream_token: str) -> str:
        """Return a freshly resolved proxy-provider-scoped access token."""
        try:
            token_data: Any = None
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_transient_exchange_error),
                stop=self.exchange_retry_stop,
                wait=self.exchange_retry_wait,
                reraise=True,
            ):
                with attempt:
                    async with AsyncOAuth2Client(
                        client_id=self._config.proxy_client_id, timeout=self._config.exchange_timeout
                    ) as exchange_client:
                        exchange_client.register_compliance_hook("access_token_response", _raise_transient_token_status)
                        token_data = await exchange_client.fetch_token(
                            url=self._config.authentik_token_endpoint(),
                            grant_type="client_credentials",
                            client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                            client_assertion=upstream_token,
                            scope=EXCHANGE_SCOPES,
                        )
            if not isinstance(token_data, Mapping):
                raise ValueError("token endpoint response is not an object")
            access_token = token_data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("token endpoint response has no access_token")
        except OAuthError as error:
            _record_backend_token_exchange_failure("oauth", error)
            raise BackendTokenExchangeError from error
        except httpx.TransportError as error:
            _record_backend_token_exchange_failure("transport", error)
            raise BackendTokenExchangeError from error
        except httpx.HTTPStatusError as error:
            _record_backend_token_exchange_failure("upstream", error)
            raise BackendTokenExchangeError from error
        except (httpx.HTTPError, ValueError) as error:
            _record_backend_token_exchange_failure("response", error)
            raise BackendTokenExchangeError from error
        return access_token


def build_authentik_backend_token_provider(exchanger: AuthentikTokenExchanger) -> BackendTokenProvider:
    """Return a FastMCP dependency that resolves the current backend token.

    ``OIDCProxy`` first swaps its locally issued wire bearer for the stored
    upstream Authentik JWT. ``CurrentAccessToken`` exposes that normalized
    token; the raw HTTP Authorization header must never be used as the client
    assertion.
    """

    current_access_token = CurrentAccessToken()

    async def backend_token(access: AccessToken = current_access_token) -> str:
        try:
            return await exchanger.exchange(access.token)
        except BackendTokenExchangeError as error:
            raise ToolError("Backend authentication failed") from error

    return backend_token
