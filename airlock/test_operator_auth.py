"""Tests for auth enforcement across MCP and operator REST endpoints.

Verifies that:
- Agent JWT sees only agent tools (propose/read), not operator tools
- Operator REST API requires decide scope
- Requests without auth or with wrong scope are rejected
"""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client

from airlock.conftest import TEST_NS, GateAppFactory, OperatorClient, agent_transport, serve_app


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
    """Agent JWT sees only agent-scoped MCP tools (no operator tools on MCP)."""
    async with Client(agent_transport(gate_http, agent_jwt)) as client:
        tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    assert {"test_echo", "withdraw_action", "list_actions"} <= tool_names
    assert {"approve_action", "reject_action"}.isdisjoint(tool_names)


async def test_operator_rest_api_requires_auth(gate_http):
    """REST API returns 401 without a Bearer token."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{gate_http}/api/actions")
    assert resp.status_code == 401


async def test_operator_rest_api_rejects_agent_jwt(gate_http, agent_jwt):
    """REST API rejects a JWT with propose scope but no decide scope."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{gate_http}/api/actions", headers={"Authorization": f"Bearer {agent_jwt}"})
    # JWTVerifier with required_scopes rejects tokens missing the decide scope
    # at the verification level (401), not as a post-verification authz check (403).
    assert resp.status_code == 401


async def test_operator_rest_api_accepts_operator_jwt(gate_http, operator_jwt):
    """REST API accepts a JWT with decide scope and returns actions."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{gate_http}/api/actions", headers={"Authorization": f"Bearer {operator_jwt}"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_operator_rest_list_actions(gate_http, agent_jwt, operator_client: OperatorClient):
    """Operator can list pending actions created by an agent via REST API."""
    async with Client(agent_transport(gate_http, agent_jwt)) as agent:
        await agent.call_tool("test_echo", {"input": {"text": "hi"}, "justification": "test", "session_key": "s1"})

    actions = await operator_client.list_actions()
    assert len(actions) >= 1


if __name__ == "__main__":
    pytest_bazel.main()
