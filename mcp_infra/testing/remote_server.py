"""Helpers for serving FastMCP instances over loopback in tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.mcp_config import RemoteMCPServer

from util.testing.asgi import serve_fastmcp


@asynccontextmanager
async def as_remote_server(server: FastMCP) -> AsyncIterator[RemoteMCPServer]:
    """Serve a FastMCP instance over HTTP and return its remote-server spec."""
    with serve_fastmcp(server) as url:
        yield RemoteMCPServer(url=url)
