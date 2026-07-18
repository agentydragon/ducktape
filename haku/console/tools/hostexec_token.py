"""Mint a per-host `hostexec` token from the operator's Authentik token (the production `MintToken`).

Built per approved call with the acting operator's own Authentik access token. `mint(host, run_as)`
runs the RFC-7523 jwt-bearer exchange (the same mechanism as
`mcp_infra.authentik_auth.token_exchange`) against the host's `hostexec-<host>` Authentik provider,
yielding a token with `aud=hostexec-<host>` and the operator's `hostexec-*` group claims. `hostexecd`
then verifies it and checks the `hostexec-<run_as>-<host>` group — so `run_as` is not part of the
exchange (the token carries whatever hostexec groups the operator holds).

Authority is the operator's real Authentik identity; there is no bespoke console key. A host with no
configured provider, or an Authentik rejection, is a clear `ToolError`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

# jwt-bearer client assertion (RFC 7523) drives Authentik's exchange, as in token_exchange.py.
_JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class HostexecJwtBearerExchanger:
    """Runs the RFC 7523 JWT-bearer exchange of the operator's login token for a per-host token."""

    def __init__(
        self,
        *,
        operator_token: str | None,
        token_endpoint: str,
        audience_client_ids: Mapping[str, str],
        scope: str,
        timeout: float = 10.0,
    ) -> None:
        # None only when the server is built for tool-schema reflection (tools/list never mints);
        # a real execution always resolves the operator's Authentik token first.
        self._operator_token = operator_token
        self._token_endpoint = token_endpoint
        self._audience_client_ids = dict(audience_client_ids)
        self._scope = scope
        self._timeout = timeout

    async def mint(self, host: str, run_as: str) -> str:
        if not self._operator_token:
            raise ToolError("no operator Authentik token available; log in to the console with offline_access")
        client_id = self._audience_client_ids.get(host)
        if client_id is None:
            raise ToolError(f"host {host!r} has no configured hostexec Authentik provider")
        # A fresh client per exchange: fetch_token mutates client-local token state (see
        # token_exchange.py). run_as is not sent — the operator's token carries the group claims.
        try:
            async with AsyncOAuth2Client(client_id=client_id, timeout=self._timeout) as exchange:
                token = await exchange.fetch_token(
                    url=self._token_endpoint,
                    grant_type="client_credentials",
                    client_assertion_type=_JWT_BEARER,
                    client_assertion=self._operator_token,
                    scope=self._scope,
                )
        except OAuthError as error:
            raise ToolError(f"minting hostexec token for {host!r} failed: {error}") from error
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ToolError(f"Authentik returned no access_token for host {host!r}")
        return access_token
