"""Helpers for serving FastMCP instances over loopback in tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.mcp_config import RemoteMCPServer
from starlette.applications import Starlette
from starlette.routing import Mount

from util.net import pick_free_port
from util.testing.asgi import serve_app


@asynccontextmanager
async def as_remote_server(server: FastMCP) -> AsyncIterator[RemoteMCPServer]:
    """Serve a FastMCP instance over HTTP and return its remote-server spec."""
    port = pick_free_port()
    mcp_app = server.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    async with serve_app(app, port=port):
        yield RemoteMCPServer(url=f"http://127.0.0.1:{port}/mcp")
