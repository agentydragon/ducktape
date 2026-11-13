"""Helpers for serving FastMCP instances over loopback in tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, contextmanager

from fastmcp import FastMCP
from fastmcp.mcp_config import RemoteMCPServer
from starlette.applications import Starlette
from starlette.routing import Mount

from util.testing.asgi import serve_app_sync


@contextmanager
def serve_fastmcp(server: FastMCP) -> Generator[str]:
    """Serve a FastMCP app and yield its streamable HTTP URL, including ``/mcp``."""
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    with serve_app_sync(app) as origin:
        yield f"{origin}/mcp"


@asynccontextmanager
async def as_remote_server(server: FastMCP) -> AsyncIterator[RemoteMCPServer]:
    """Serve a FastMCP instance over HTTP and return its remote-server spec."""
    with serve_fastmcp(server) as url:
        yield RemoteMCPServer(url=url)
