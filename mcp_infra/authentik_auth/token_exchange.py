"""Request-scoped backend token exchange (RFC 7523 JWT-bearer client assertion) against Authentik.

The exchange mechanism is the vendor-neutral JWT-bearer grant; this module binds it to Authentik
(``AuthentikAuthConfig``, the ``ak_proxy`` scope, the outpost identity headers).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.dependencies import CurrentAccessToken
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from prometheus_client import Counter
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from mcp_infra.authentik_auth.config import AuthentikAuthConfig

# Scopes requested when exchanging the caller's token for a proxy-scoped one.
# Each is load-bearing — see x/authentik_mcp_poc/NOTES.md §6 for why `ak_proxy`
# is required (without it the outpost forwards empty identity headers).
EXCHANGE_SCOPES = "openid email profile ak_proxy"

BackendTokenProvider = Callable[..., Awaitable[str]]

BACKEND_TOKEN_EXCHANGE_FAILURES = Counter(
    "mcp_auth_backend_token_exchange_failures_total",
    "Backend token exchanges that failed before tool execution",
    ["outcome"],  # oauth | transport | upstream | response
)

logger = logging.getLogger(__name__)


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
