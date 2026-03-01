"""Tests for MCP tool visibility with different auth credentials.

Verifies that the agent bearer token cannot see operator-only MCP tools,
while a valid Authentik admin JWT can. Uses a real HTTP server to ensure
FastMCP's auth enforcement works end-to-end.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
import pytest_bazel
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.mcp_config import RemoteMCPServer
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.routing import Mount

from approval_gate.mcp_auth import ApprovalGateAuthProvider
from approval_gate.predicates import NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from mcp_infra.prefix import MCPMountPrefix
from util.net import pick_free_port

_AGENT_API_KEY = "test-agent-bearer-key"
_TEST_NS = MCPMountPrefix("test")


@pytest.fixture
def rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def admin_jwt(rsa_private_key):
    return pyjwt.encode({"sub": "admin"}, rsa_private_key, algorithm="RS256")


@pytest.fixture
def mock_jwks_signing_key(rsa_private_key):
    key = MagicMock()
    key.key = rsa_private_key.public_key()
    return key


@asynccontextmanager
async def _serve_app(app, *, port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = time.monotonic() + 10.0
    while not server.started:
        if task.done():
            try:
                task.result()
            except Exception as exc:
                raise RuntimeError(f"uvicorn exited: {exc}") from exc
            raise RuntimeError("uvicorn exited before starting")
        if time.monotonic() > deadline:
            server.should_exit = True
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise TimeoutError(f"server did not start on port {port}")
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def gate_http(tmp_path, mock_jwks_signing_key):
    """HTTP gate with an in-process FastMCP backend; yields (gate_url, agent_key)."""
    backend = FastMCP("test-backend")

    @backend.tool()
    async def echo(text: str) -> str:
        return f"echoed: {text}"

    jwks_client = PyJWKClient("http://test/jwks")
    auth = ApprovalGateAuthProvider(agent_api_key=_AGENT_API_KEY, jwks_client=jwks_client)
    gate = ApprovalGateServer(
        backends={_TEST_NS: backend},
        db_path=tmp_path / "gate.db",
        predicate=lambda ns, tool, args: NeedsHumanDecision(),
        public_base_url="http://test",
        auth=auth,
    )
    mcp_app = gate.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    gate_port = pick_free_port()
    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=mock_jwks_signing_key):
        async with _serve_app(app, port=gate_port):
            yield f"http://127.0.0.1:{gate_port}", _AGENT_API_KEY


@pytest.fixture
async def agent_tool_names(gate_http) -> set[str]:
    """Tool names visible to the agent bearer token."""
    gate_url, agent_key = gate_http
    transport = RemoteMCPServer(url=f"{gate_url}/mcp", headers={"Authorization": f"Bearer {agent_key}"}).to_transport()
    async with Client(transport) as client:
        tools = await client.list_tools()
    return {t.name for t in tools}


@pytest.fixture
async def operator_tool_names(gate_http, admin_jwt) -> set[str]:
    """Tool names visible to a valid Authentik admin JWT."""
    gate_url, _ = gate_http
    transport = RemoteMCPServer(url=f"{gate_url}/mcp", headers={"x-authentik-jwt": admin_jwt}).to_transport()
    async with Client(transport) as client:
        tools = await client.list_tools()
    return {t.name for t in tools}


async def test_agent_cannot_see_operator_tools(agent_tool_names):
    """Agent bearer token must not see approve_action/reject_action."""
    assert "approve_action" not in agent_tool_names
    assert "reject_action" not in agent_tool_names
    assert "test_echo" in agent_tool_names
    assert "withdraw_action" in agent_tool_names
    # list_actions requires reader scope which both roles have
    assert "list_actions" in agent_tool_names


async def test_operator_jwt_sees_operator_tools(operator_tool_names):
    """Valid Authentik admin JWT must expose operator MCP tools."""
    assert "approve_action" in operator_tool_names
    assert "reject_action" in operator_tool_names
    assert "list_actions" in operator_tool_names
    assert "test_echo" not in operator_tool_names
    assert "withdraw_action" not in operator_tool_names


if __name__ == "__main__":
    pytest_bazel.main()
