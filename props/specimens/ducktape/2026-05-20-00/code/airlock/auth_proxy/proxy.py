"""Auth proxy: MCP protocol proxy with OAuth2 client_credentials token injection.

Uses FastMCP's StatefulProxyClient to proxy MCP operations at the protocol level
(not raw HTTP). Token management is delegated to authlib's AsyncOAuth2Client
via StreamableHttpTransport's httpx_client_factory — the OAuth2 client IS the
HTTP client, so token fetch/cache/refresh happens transparently on every request.

Designed as a sidecar container in the OpenClaw gateway pod so the OpenClaw
plugin can reach the Airlock without managing OAuth2 tokens itself.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import httpx
import uvicorn
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import FastMCPProxy, StatefulProxyClient
from starlette.applications import Starlette

from util.env import get_required_env

logger = logging.getLogger(__name__)


class AutoFetchOAuth2Client(AsyncOAuth2Client):
    """AsyncOAuth2Client that auto-fetches a token on first request.

    AsyncOAuth2Client raises MissingTokenError if no token exists when
    request() is called. This subclass fetches one automatically via the
    client_credentials grant before delegating to the parent.
    """

    async def request(self, method, url, withhold_token=False, auth=httpx.USE_CLIENT_DEFAULT, **kwargs):
        if not withhold_token and auth is httpx.USE_CLIENT_DEFAULT and not self.token:
            await self.fetch_token(self.metadata["token_endpoint"], grant_type="client_credentials")
        return await super().request(method, url, withhold_token=withhold_token, auth=auth, **kwargs)


def oauth2_client_factory(
    token_url: str, client_id: str, client_secret: str, scope: str
) -> Callable[..., AutoFetchOAuth2Client]:
    """Create an httpx_client_factory for StreamableHttpTransport.

    Returns a factory that produces an AutoFetchOAuth2Client configured for
    client_credentials grant. The transport passes headers, follow_redirects,
    and timeout as kwargs; we forward them to the underlying httpx.AsyncClient.
    """

    def factory(**kwargs: object) -> AutoFetchOAuth2Client:
        # Transport passes auth= from its own auth field; we don't need it
        # since the OAuth2 client handles auth internally.
        kwargs.pop("auth", None)
        return AutoFetchOAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            token_endpoint_auth_method="client_secret_post",
            token_endpoint=token_url,
            grant_type="client_credentials",
            **kwargs,
        )

    return factory


def create_app(upstream_url: str, client_factory: Callable[..., AutoFetchOAuth2Client]) -> Starlette:
    """Build ASGI app that MCP-proxies to upstream with OAuth2 auth."""
    transport = StreamableHttpTransport(upstream_url, httpx_client_factory=client_factory)
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

    factory = oauth2_client_factory(token_url=token_url, client_id=client_id, client_secret=client_secret, scope=scope)
    app = create_app(upstream_url, factory)
    logger.info("auth proxy listening on %s:%d, upstream=%s", host, port, upstream_url)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
