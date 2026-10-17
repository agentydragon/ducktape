"""MCP-dependent pytest fixtures for agent tests.

Register in downstream packages via conftest.py:
    from agent_core.testing.mcp.fixtures import *  # noqa: F403
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from fastmcp.client import Client
from fastmcp.server import FastMCP

from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.mcp.echo_server import make_echo_server
from agent_core.tool_provider import ToolProvider

# ---- MCP fixtures ----


@pytest.fixture
def echo_server() -> FastMCP:
    """Echo FastMCP server instance."""
    return make_echo_server()


@pytest.fixture
async def mcp_client_echo(echo_server):
    """Plain MCP client with echo server (no policy gateway).

    For tests that need a simple MCP server but not a policy gateway.
    """
    async with Client(echo_server) as client:
        yield client


@pytest.fixture
async def mcp_tool_provider() -> AsyncGenerator[ToolProvider]:
    """Provider connected to an empty server for tests that make no MCP calls."""
    async with Client(FastMCP("empty")) as client:
        yield MCPToolProvider(client)


@pytest.fixture
def mcp_tool_provider_echo(mcp_client_echo) -> ToolProvider:
    """MCPToolProvider wrapping the echo-only client."""
    return MCPToolProvider(mcp_client_echo)
