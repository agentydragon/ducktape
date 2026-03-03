"""Auth proxy: MCP protocol proxy with OAuth2 client_credentials token injection.

Uses FastMCP's StatefulProxyClient to proxy MCP operations at the protocol level
(not raw HTTP). Token management is delegated to authlib's AsyncOAuth2Client
which handles the client_credentials grant with automatic refresh.

Designed as a sidecar container in the OpenClaw gateway pod so the OpenClaw
plugin can reach the approval gate without managing OAuth2 tokens itself.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator

import httpx
import uvicorn
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import FastMCPProxy, StatefulProxyClient
from starlette.applications import Starlette

from util.env import get_required_env

logger = logging.getLogger(__name__)


class ClientCredentialsAuth(httpx.Auth):
    """httpx auth flow for OAuth2 client_credentials using authlib."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str) -> None:
        self._token_url = token_url
        self._oauth = AsyncOAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            token_endpoint_auth_method="client_secret_post",
            token_endpoint=token_url,
            grant_type="client_credentials",
        )

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if not self._oauth.token:
            await self._oauth.fetch_token(self._token_url, grant_type="client_credentials")
        else:
            await self._oauth.ensure_active_token(self._oauth.token)
        request.headers["Authorization"] = f"Bearer {self._oauth.token['access_token']}"
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
