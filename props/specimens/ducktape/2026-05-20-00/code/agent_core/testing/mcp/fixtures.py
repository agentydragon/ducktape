"""MCP-dependent pytest fixtures for agent tests.

Register in downstream packages via conftest.py:
    from agent_core.testing.mcp.fixtures import *  # noqa: F403
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.server import FastMCP

from agent_core.events import ToolCall
from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.mcp.echo_server import make_echo_server
from agent_core.tool_provider import ToolProvider
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

# ---- MCP fixtures ----
# Note: compositor and compositor_client fixtures are in mcp_infra.testing.fixtures


@pytest.fixture
def echo_server() -> FastMCP:
    """Echo FastMCP server instance."""
    return make_echo_server()


@pytest.fixture
def echo_spec(echo_server) -> dict[str, FastMCP]:
    """In-proc FastMCP server spec for echo tests."""
    return {"echo": echo_server}


@pytest.fixture
async def mcp_client_echo(make_compositor, echo_spec):
    """Plain MCP client with echo server (no policy gateway).

    For tests that don't need policy approval but need a simple MCP server.
    Using plain Compositor avoids Docker overhead and potential timeouts.

    Note: Requires mcp_infra.testing.fixtures to be registered for make_compositor.
    """
    async with make_compositor(echo_spec) as (client, _comp):
        yield client


@pytest.fixture
def mcp_tool_provider(compositor_client) -> ToolProvider:
    """MCPToolProvider wrapping compositor_client.

    Use this fixture instead of manually wrapping compositor_client.
    """
    return MCPToolProvider(compositor_client)


@pytest.fixture
def mcp_tool_provider_echo(mcp_client_echo) -> ToolProvider:
    """MCPToolProvider wrapping echo-only client (no compositor)."""
    return MCPToolProvider(mcp_client_echo)


# ---- Event/call factories ----


@pytest.fixture
def make_tool_call(call_id_gen: Callable[[], str]) -> Callable[..., ToolCall]:
    """Factory for ToolCall events with auto call_id generation."""

    def _make(server: MCPMountPrefix, tool: str, args: dict[str, Any] | None = None) -> ToolCall:
        args_json = json.dumps(args) if args is not None else None
        return ToolCall(name=build_mcp_function(server, tool), args_json=args_json, call_id=call_id_gen())

    return _make
