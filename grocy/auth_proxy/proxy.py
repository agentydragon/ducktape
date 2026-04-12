"""HTTP reverse proxy that injects Authentik M2M Bearer tokens for Grocy.

Sits between a Grocy MCP server (which sends GROCY-API-KEY) and the real
Grocy instance behind an Authentik proxy outpost (which requires Bearer JWT).
Strips the GROCY-API-KEY header, obtains a JWT via Authentik's M2M flow
(username + app_password + client_id), and forwards with Authorization: Bearer.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import httpx
import uvicorn
from authlib.integrations.httpx_client import AsyncOAuth2Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from util.env import get_required_env

logger = logging.getLogger(__name__)

# Headers stripped from inbound requests before forwarding:
# - grocy-api-key: the whole point — callers send a dummy key, we replace with JWT.
# - host: httpx sets its own based on the upstream URL.
# - authorization / proxy-authorization: prevent callers from bypassing our
#   Bearer injection by supplying their own credentials to the upstream.
_STRIP_REQUEST_HEADERS = frozenset({"grocy-api-key", "host", "authorization", "proxy-authorization"})

# Hop-by-hop headers (RFC 7230 §6.1) must not be forwarded between hops.
# Stripped from upstream responses.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


class AutoFetchOAuth2Client(AsyncOAuth2Client):
    """AsyncOAuth2Client that auto-fetches a token via M2M app_password flow.

    Authentik M2M for proxy providers uses username + app_password, not
    client_id + client_secret. The client_id identifies the provider;
    authentication is user-based.
    """

    async def request(self, method, url, withhold_token=False, auth=httpx.USE_CLIENT_DEFAULT, **kwargs):
        if not withhold_token and auth is httpx.USE_CLIENT_DEFAULT and not self.token:
            await self.fetch_token(
                self.metadata["token_endpoint"],
                grant_type="client_credentials",
                username=self.metadata["m2m_username"],
                password=self.metadata["m2m_password"],
            )
        return await super().request(method, url, withhold_token=withhold_token, auth=auth, **kwargs)


def create_client(token_url: str, client_id: str, username: str, app_password: str) -> AutoFetchOAuth2Client:
    """Create an OAuth2 HTTP client for the Authentik M2M flow.

    Authentication method is "none": we don't have a client_secret. Authentik
    identifies the client via client_id and authenticates the *user* via
    username + app_password (sent as form params alongside
    grant_type=client_credentials).
    """
    return AutoFetchOAuth2Client(
        client_id=client_id,
        token_endpoint_auth_method="none",
        token_endpoint=token_url,
        grant_type="client_credentials",
        metadata={"token_endpoint": token_url, "m2m_username": username, "m2m_password": app_password},
    )


def create_app(upstream_url: str, client_factory: Callable[[], AutoFetchOAuth2Client]) -> Starlette:
    """Build a Starlette app that reverse-proxies to upstream with Bearer auth."""
    # Normalize once so path concatenation never produces `//`.
    upstream_url = upstream_url.rstrip("/")

    client: AutoFetchOAuth2Client | None = None

    async def lifespan(app):
        nonlocal client
        client = client_factory()
        yield
        await client.aclose()

    async def health(request: Request) -> Response:
        return Response('{"status":"ok"}', media_type="application/json")

    async def proxy(request: Request) -> Response:
        assert client is not None
        url = f"{upstream_url}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS]
        body = await request.body()

        resp = await client.request(request.method, url, content=body, headers=headers)

        # Construct with no headers, then assign raw_headers directly so we can
        # preserve repeated headers (e.g. multiple Set-Cookie) via multi_items.
        # Drop hop-by-hop headers which must not cross proxies.
        out = Response(content=resp.content, status_code=resp.status_code)
        out.raw_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in resp.headers.multi_items()
            if k.lower() not in _HOP_BY_HOP_HEADERS
        ]
        return out

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)

    upstream_url = get_required_env("UPSTREAM_URL")
    token_url = get_required_env("TOKEN_URL")
    client_id = get_required_env("CLIENT_ID")
    username = get_required_env("USERNAME")
    app_password = get_required_env("APP_PASSWORD")
    port = int(get_required_env("PORT"))

    app = create_app(upstream_url, lambda: create_client(token_url, client_id, username, app_password))
    logger.info("grocy auth proxy listening on 0.0.0.0:%d, upstream=%s", port, upstream_url)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
