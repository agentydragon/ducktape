"""Auth proxy: MCP protocol proxy with OAuth2 client_credentials token injection.

Uses FastMCP's StatefulProxyClient to proxy MCP operations at the protocol level
(not raw HTTP). A custom httpx.Auth subclass handles OAuth2 client_credentials
grant with automatic token refresh.

Designed as a sidecar container in the OpenClaw gateway pod so the OpenClaw
plugin can reach the approval gate without managing OAuth2 tokens itself.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncGenerator

import httpx
import uvicorn
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import FastMCPProxy, StatefulProxyClient
from starlette.applications import Starlette

from util.env import get_required_env

logger = logging.getLogger(__name__)


class ClientCredentialsAuth(httpx.Auth):
    """httpx auth flow for OAuth2 client_credentials grant with auto-refresh."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_expired(self) -> bool:
        # 30s safety margin before actual expiry
        return self._access_token is None or time.monotonic() >= self._expires_at - 30

    async def _fetch_token(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": self._scope,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._expires_at = time.monotonic() + data.get("expires_in", 300)
            logger.info("Fetched new OAuth2 token (expires_in=%s)", data.get("expires_in"))

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        async with self._lock:
            if self._is_expired():
                await self._fetch_token()
        request.headers["Authorization"] = f"Bearer {self._access_token}"
        yield request


def create_app(upstream_url: str, auth: ClientCredentialsAuth) -> Starlette:
    """Build ASGI app that MCP-proxies to upstream with OAuth2 auth."""
    transport = StreamableHttpTransport(upstream_url, auth=auth)
    client: StatefulProxyClient[StreamableHttpTransport] = StatefulProxyClient(transport=transport)
    proxy = FastMCPProxy(client_factory=client.new_stateful, name="auth-proxy")
    return proxy.http_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)

    upstream_url = get_required_env("UPSTREAM_URL")
    token_url = get_required_env("TOKEN_URL")
    client_id = get_required_env("CLIENT_ID")
    client_secret = get_required_env("CLIENT_SECRET")
    scope = get_required_env("SCOPE")
    host = "127.0.0.1"
    port = int(get_required_env("PORT"))

    auth = ClientCredentialsAuth(token_url=token_url, client_id=client_id, client_secret=client_secret, scope=scope)
    app = create_app(upstream_url, auth)
    logger.info("auth proxy listening on %s:%d, upstream=%s", host, port, upstream_url)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
