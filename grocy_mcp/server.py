"""FastMCP server for auth-aware Grocy.

Authenticates remote MCP clients via Authentik (OIDCProxy) and then uses the
caller's identity to drive Grocy's REST API, whose ingress sits behind an
Authentik proxy provider outpost.

This is deliberately a thin wrapper: FastMCP's OpenAPI provider generates the
tool surface directly from Grocy's OpenAPI 3.1 spec. A request-scoped
dependency resolves the MCP user's upstream Authentik JWT to a
Grocy-proxy-scoped client before each tool body runs. `TOOL_OVERRIDES` controls
naming, descriptions, and enablement.

See <grocy_mcp/README.md> for the architecture and end-to-end flow;
<x/authentik_mcp_poc/NOTES.md> §5-§6 for why each piece of the token
exchange is load-bearing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.server.providers.openapi import (
    MCPType,
    OpenAPIProvider,
    OpenAPIResource,
    OpenAPIResourceTemplate,
    OpenAPITool,
    RouteMap,
)
from fastmcp.utilities.openapi import HTTPRoute
from prometheus_client import Gauge, start_http_server

from grocy_mcp.batch_tools import register_batch_tools
from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.tool_metadata import TOOL_OVERRIDES
from mcp_infra.authentik_auth.auth import (
    AuthentikTokenExchanger,
    build_authentik_auth,
    build_authentik_backend_token_provider,
)
from mcp_infra.persistence import build_client_storage
from mcp_infra.request_scoped_openapi import HTTPClientProvider, RequestScopedOpenAPIClients
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_TOOLS = Gauge("grocy_mcp_tools", "Number of tools advertised by the Grocy MCP server")

# All routes become tools by default; TOOL_OVERRIDES controls which are
# enabled or disabled.
ROUTE_MAPS = [RouteMap(pattern=r".*", mcp_type=MCPType.TOOL)]


def _load_openapi_spec() -> dict[str, object]:
    """Return the pre-fixed Grocy OpenAPI 3.1 spec.

    The raw upstream spec has issues (empty enums, dangling $refs for
    entity path parameters) that are fixed at build time by the
    :grocy_openapi_fixed genrule. See <fix_openapi_spec.py>.
    """
    spec_path = get_required_path("_main/grocy_mcp/grocy.openapi.fixed.json")
    result: dict[str, object] = json.loads(spec_path.read_text())
    return result


def _load_server_instructions() -> str:
    """Return the shared cross-cutting-conventions markdown.

    Delivered to MCP clients via the `initialize.instructions` field. See
    <server_instructions.md> for the content; the file is data-dep'd into
    the server target.
    """
    return get_required_path("_main/grocy_mcp/server_instructions.md").read_text()


def _authentik_client_provider(
    settings: ServerSettings, exchanger: AuthentikTokenExchanger
) -> HTTPClientProvider[GrocyClient]:
    """Build the production dependency that resolves a Grocy client per call."""
    assert settings.auth is not None
    backend_token_provider = build_authentik_backend_token_provider(exchanger)
    backend_token_dependency = Depends(backend_token_provider)

    @asynccontextmanager
    async def grocy_client(backend_token: str = backend_token_dependency) -> AsyncIterator[GrocyClient]:
        async with GrocyClient(
            base_url=f"{settings.grocy_url.rstrip('/')}/api",
            headers={"Authorization": f"Bearer {backend_token}"},
            timeout=settings.grocy_timeout,
        ) as client:
            yield client

    return grocy_client


def build_mcp(settings: ServerSettings, *, client_provider: HTTPClientProvider[GrocyClient]) -> FastMCP:
    """Build the FastMCP instance with generated Grocy tools, but no auth.

    Every caller supplies the same request-scoped provider abstraction.
    Production resolves an authenticated client per invocation; tests and local
    tooling may adapt a caller-owned client with ``borrowed_http_client_provider``.
    """
    spec = _load_openapi_spec()

    def _filter_disabled(route: HTTPRoute, mcp_type: MCPType) -> MCPType | None:
        override = TOOL_OVERRIDES.get((route.method, route.path))
        if override is not None and not override.enabled:
            return MCPType.EXCLUDE
        return mcp_type

    def _customize_component(
        route: HTTPRoute, component: OpenAPITool | OpenAPIResource | OpenAPIResourceTemplate
    ) -> None:
        override = TOOL_OVERRIDES.get((route.method, route.path))
        if override is None:
            raise ValueError(
                f"No tool override for {route.method} {route.path} — add it to TOOL_OVERRIDES in tool_metadata.py"
            )
        component.name = override.name
        if override.extra_description:
            component.description = f"{component.description}\n\n{override.extra_description}"
        # Strip output schemas — Grocy's response schemas are unreliable (wrong types,
        # null where string expected, etc.). E2e tests are the real contract.
        if isinstance(component, OpenAPITool):
            component.output_schema = None

    openapi_provider = OpenAPIProvider(
        openapi_spec=spec, route_maps=ROUTE_MAPS, route_map_fn=_filter_disabled, mcp_component_fn=_customize_component
    )
    openapi_provider.add_transform(RequestScopedOpenAPIClients(client_provider))
    mcp = FastMCP(name="Grocy", instructions=_load_server_instructions(), providers=[openapi_provider])
    register_batch_tools(mcp, settings, client_provider=client_provider)
    return mcp


def build_server(settings: ServerSettings) -> FastMCP:
    if settings.auth is None:
        raise ValueError("build_server requires ServerSettings.auth to be set (production path)")
    store = build_client_storage(settings.persistence)
    exchanger = AuthentikTokenExchanger(settings.auth)
    mcp = build_mcp(settings, client_provider=_authentik_client_provider(settings, exchanger))
    mcp.auth = build_authentik_auth(settings.auth, client_storage=store)
    return mcp


async def record_tool_count(mcp: FastMCP) -> int:
    tool_count = len(await mcp.list_tools())
    _TOOLS.set(tool_count)
    return tool_count


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = ServerSettings()
    mcp = build_server(settings)
    tool_count = asyncio.run(record_tool_count(mcp))
    app = mcp.http_app(path="/mcp")
    # Metrics (incl. mcp_auth_upstream_refresh_failures_total) on a dedicated
    # cluster-internal port, off the public HTTPRoute.
    start_http_server(settings.metrics_port, addr=settings.host)
    logger.info(
        "grocy-mcp listening on %s:%d, metrics on :%d, tools=%d",
        settings.host,
        settings.port,
        settings.metrics_port,
        tool_count,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
