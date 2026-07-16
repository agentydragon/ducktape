"""FastMCP proxy compatibility and refresh resilience."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from authlib.oauth2 import OAuth2Error
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from glide_shared.exceptions import TimeoutError as GlideTimeoutError
from mcp.server.auth.provider import TokenError
from prometheus_client import Counter
from starlette.exceptions import HTTPException
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from mcp.server.auth.provider import RefreshToken
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

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


class RetryableRefreshOIDCProxy(OIDCProxy):
    """Keep transient upstream refresh failures retryable for MCP clients.

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
    exception — pinned by test_fastmcp_proxy.py against the locked FastMCP version.
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


class DownstreamClientIdentityOIDCProxy(RetryableRefreshOIDCProxy):
    """Restore the downstream DCR client identity after FastMCP's token swap."""

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Reattach the identity from the signed FastMCP reference token.

        ``OAuthProxy.load_access_token`` returns the upstream verifier's result,
        whose ``client_id`` is the proxy's upstream client. The FastMCP reference
        JWT retains the downstream DCR ``client_id`` needed by Haku.
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
