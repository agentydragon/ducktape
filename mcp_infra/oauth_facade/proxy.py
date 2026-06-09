"""FastMCP proxy wiring for the generic OAuth facade."""

from __future__ import annotations

import os

from fastmcp.client.transports import ClientTransport, StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

from mcp_infra.oauth_facade.config import FacadeSettings, HttpUpstream, StdioUpstream


def build_transport(settings: FacadeSettings) -> ClientTransport:
    """Build the client transport to the configured upstream MCP server.

    Shared by the proxy and the standalone upstream health probe so both reach
    the upstream identically (same URL, same server-held bearer token).
    """
    upstream = settings.upstream
    match upstream:
        case HttpUpstream():
            return StreamableHttpTransport(upstream.url, auth=upstream.bearer_token)
        case StdioUpstream():
            return StdioTransport(command=upstream.command[0], args=upstream.command[1:], env=os.environ.copy())


def build_proxy_server(settings: FacadeSettings, **kwargs: object):
    """Create a FastMCP proxy to the configured upstream MCP server."""
    return create_proxy(
        ProxyClient(build_transport(settings)), name=settings.facade_name, instructions=settings.instructions, **kwargs
    )
