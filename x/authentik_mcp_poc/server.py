"""FastMCP server for the Authentik MCP POC.

Wires OIDCProxy + JWTVerifier against Authentik (via shared
`mcp_infra.authentik_auth`) and exposes a single tool, `whoami_via_backend`,
that forwards the caller's Bearer token to the proxy-outpost-protected whoami
backend.

See <x/authentik_mcp_poc/README.md> for the end-to-end flow.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from mcp_infra.authentik_auth.auth import (
    AuthentikTokenExchanger,
    build_authentik_auth,
    build_authentik_backend_token_provider,
)
from x.authentik_mcp_poc.config import ServerSettings

logger = logging.getLogger(__name__)


def build_server(settings: ServerSettings) -> FastMCP:
    auth_config = settings.auth_config()
    exchanger = AuthentikTokenExchanger(auth_config)
    backend_token_provider = build_authentik_backend_token_provider(exchanger)
    backend_token_dependency = Depends(backend_token_provider)

    @asynccontextmanager
    async def backend_client(backend_token: str = backend_token_dependency) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url=settings.backend_url.rstrip("/"),
            headers={"Authorization": f"Bearer {backend_token}"},
            timeout=10.0,
        ) as client:
            yield client

    mcp: FastMCP = FastMCP(
        name="Authentik MCP POC",
        instructions=(
            "POC MCP server for Authentik-authenticated remote MCP. "
            "Call whoami_via_backend to see your identity flow through an "
            "Authentik proxy outpost to a downstream service."
        ),
        auth=build_authentik_auth(auth_config),
    )
    backend_client_dependency = Depends(backend_client)

    @mcp.tool
    async def whoami_via_backend(client: httpx.AsyncClient = backend_client_dependency) -> dict[str, object]:
        """Call the Authentik-proxy-protected whoami backend as the current user.

        Exchanges the user's upstream Authentik token for one scoped to the
        backend's proxy provider, then forwards it to `/whoami`. The outpost
        validates via introspection, injects `X-Authentik-*` identity headers,
        and the backend echoes them back.
        """
        response = await client.get("/whoami")
        return {
            "backend_status": response.status_code,
            "backend_url": str(response.request.url),
            "backend_response": response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else response.text,
        }

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = ServerSettings()
    mcp = build_server(settings)
    app = mcp.http_app(path="/mcp")
    logger.info("authentik-mcp-poc listening on %s:%d", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
