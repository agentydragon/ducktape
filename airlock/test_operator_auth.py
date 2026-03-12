"""Tests for MCP tool visibility with different auth credentials.

Verifies that an agent JWT (scopes: propose, read) cannot see operator-only
tools, while an operator JWT (scopes: decide, read) can. Uses a real HTTP
server to ensure FastMCP's auth enforcement works end-to-end.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client

from airlock.conftest import TEST_NS, GateAppFactory, agent_transport, operator_transport, serve_app


@pytest.fixture
async def gate_http(make_gate_app: GateAppFactory, free_port: int):
    """HTTP gate with an in-process FastMCP backend; yields base URL."""
    backend = FastMCP()

    @backend.tool()
    async def echo(text: str) -> str:
        return f"echoed: {text}"

    app = make_gate_app({TEST_NS: backend})
    async with serve_app(app, port=free_port):
        yield f"http://127.0.0.1:{free_port}"


async def test_agent_cannot_see_operator_tools(gate_http, agent_jwt):
    """Agent JWT must not see approve_action/reject_action."""
    async with Client(agent_transport(gate_http, agent_jwt)) as client:
        tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    assert {"test_echo", "withdraw_action", "list_actions"} <= tool_names
    assert {"approve_action", "reject_action"}.isdisjoint(tool_names)


async def test_operator_jwt_sees_operator_tools(gate_http, operator_jwt):
    """Operator JWT must expose operator MCP tools."""
    async with Client(operator_transport(gate_http, operator_jwt)) as client:
        tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    assert {"approve_action", "reject_action", "list_actions"} <= tool_names
    assert {"test_echo", "withdraw_action"}.isdisjoint(tool_names)


if __name__ == "__main__":
    pytest_bazel.main()
