"""FastMCP server exposing IBKR's read-only market-data + session tools.

A thin wrapper: FastMCP's ``OpenAPIProvider`` generates the tool surface
directly from IBKR's own Client Portal Web API spec (filtered to the read-only
allowlist and transcoded to OpenAPI 3.1 at build time — see <spec_fixup.py>).
Every generated tool is bound to a single long-lived HTTP client pointed at the
co-located Client Portal Gateway, which holds the one authenticated (paper)
session. There is no per-caller identity: the gateway *is* the identity, and it
is read-only by construction.

Front-door auth (claude.ai / Claude Code users, plus Haku's machine JWT) is the
same Authentik ``OIDCProxy`` + ``direct_jwt_trusts`` pattern as <../grocy_mcp/>.

See <ibkr_mcp/README.md> for the architecture and the free-tier / weekly-reauth
flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx
import uvicorn
from fastmcp import FastMCP
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

from ibkr_mcp.mcp_types import ServerSettings
from ibkr_mcp.route_policy import READ_ONLY_OPERATIONS
from mcp_infra.authentik_auth.provider import build_authentik_auth
from mcp_infra.persistence import build_client_storage
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_TOOLS = Gauge("ibkr_mcp_tools", "Number of tools advertised by the IBKR MCP server")

# The build-time-filtered spec already contains only allowlisted routes, so
# every remaining route becomes a tool.
ROUTE_MAPS = [RouteMap(pattern=r".*", mcp_type=MCPType.TOOL)]

_INSTRUCTIONS = (
    "Read-only Interactive Brokers market data via the Client Portal Web API. "
    "Resolve a symbol to a contract id with `secdef_search`, then read quotes with "
    "`market_data_snapshot` or bars with `market_data_history` (delayed on the free tier). "
    "Check the session with `session_status`; if it is not authenticated, call `request_reauth` "
    "to trigger the IBKR Mobile push the account holder approves on their phone. "
    "This server places no orders and has no access to any trading route."
)


def _load_openapi_spec() -> dict[str, object]:
    """Return the read-only OpenAPI 3.1 spec produced by the `ibkr_openapi_fixed` genrule."""
    spec_path = get_required_path("_main/ibkr_mcp/ibkr.openapi.fixed.json")
    result: dict[str, object] = json.loads(spec_path.read_text())
    return result


def build_gateway_client(settings: ServerSettings) -> httpx.AsyncClient:
    """A single long-lived client to the co-located gateway; its cookie jar carries the session."""
    return httpx.AsyncClient(
        base_url=settings.gateway_base_url, verify=settings.gateway_verify_tls, timeout=settings.gateway_timeout
    )


def build_mcp(settings: ServerSettings, *, client: httpx.AsyncClient) -> FastMCP:
    """Build the FastMCP instance with generated IBKR tools, but no front-door auth."""
    spec = _load_openapi_spec()

    def _customize_component(
        route: HTTPRoute, component: OpenAPITool | OpenAPIResource | OpenAPIResourceTemplate
    ) -> None:
        key = (route.method, route.path)
        tool_spec = READ_ONLY_OPERATIONS.get(key)
        if tool_spec is None:
            # Defense in depth: the filtered spec should make this unreachable.
            raise ValueError(f"Refusing to surface non-allowlisted IBKR operation {route.method} {route.path}")
        component.name = tool_spec.name
        if tool_spec.extra_description:
            base = component.description or ""
            component.description = f"{base}\n\n{tool_spec.extra_description}".strip()
        # Keep IBKR's response schemas as output schemas — verify they're reliable once
        # we can exercise the live gateway (TODO.md), and only strip if they misbehave.

    # The gateway holds a single fixed session, so one client is bound to every
    # generated tool at construction — no per-caller client swapping (unlike
    # grocy_mcp, which resolves a per-user client via a request-scoped transform).
    provider = OpenAPIProvider(
        openapi_spec=spec, client=client, route_maps=ROUTE_MAPS, mcp_component_fn=_customize_component
    )
    return FastMCP(name="IBKR Market Data", instructions=_INSTRUCTIONS, providers=[provider])


def build_server(settings: ServerSettings) -> FastMCP:
    if settings.auth is None:
        raise ValueError("build_server requires ServerSettings.auth to be set (production path)")
    store = build_client_storage(settings.persistence)
    mcp = build_mcp(settings, client=build_gateway_client(settings))
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
    start_http_server(settings.metrics_port, addr=settings.host)
    logger.info(
        "ibkr-mcp listening on %s:%d, metrics on :%d, tools=%d",
        settings.host,
        settings.port,
        settings.metrics_port,
        tool_count,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
