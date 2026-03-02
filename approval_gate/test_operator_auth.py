"""Tests for MCP tool visibility with different auth credentials.

Verifies that an agent JWT (scopes: propose, read) cannot see operator-only
tools, while an operator JWT (scopes: decide, read) can. Uses a real HTTP
server to ensure FastMCP's auth enforcement works end-to-end.

Both roles now authenticate via JWT verified against the same JWKS. The
``x-authentik-jwt`` header is normalized to ``Authorization: Bearer`` by
``AuthentikHeaderNormalizer`` before reaching ``JWTVerifier``.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress

import pytest
import pytest_bazel
import uvicorn
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.mcp_config import RemoteMCPServer
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.routing import Mount

from approval_gate.mcp_auth import AuthentikHeaderNormalizer
from approval_gate.predicates import NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from mcp_infra.prefix import MCPMountPrefix
from util.net import pick_free_port

_TEST_NS = MCPMountPrefix("test")


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
async def gate_http(tmp_path, rsa_key_pair):
    """HTTP gate with an in-process FastMCP backend; yields gate_url."""
    backend = FastMCP("test-backend")

    @backend.tool()
    async def echo(text: str) -> str:
        return f"echoed: {text}"

    auth = JWTVerifier(public_key=rsa_key_pair.public_key)
    gate = ApprovalGateServer(
        backends={_TEST_NS: backend},
        db_path=tmp_path / "gate.db",
        predicate=lambda ns, tool, args: NeedsHumanDecision(),
        public_base_url="http://test",
        auth=auth,
    )
    mcp_app = gate.http_app(path="/")
    mcp_app_with_header_norm = AuthentikHeaderNormalizer(mcp_app)
    app = Starlette(routes=[Mount("/mcp", app=mcp_app_with_header_norm)], lifespan=mcp_app.lifespan)
    gate_port = pick_free_port()
    async with _serve_app(app, port=gate_port):
        yield f"http://127.0.0.1:{gate_port}"


@pytest.fixture
async def agent_tool_names(gate_http, agent_jwt) -> set[str]:
    """Tool names visible to the agent JWT (propose + read scopes)."""
    transport = RemoteMCPServer(url=f"{gate_http}/mcp", headers={"Authorization": f"Bearer {agent_jwt}"}).to_transport()
    async with Client(transport) as client:
        tools = await client.list_tools()
    return {t.name for t in tools}


@pytest.fixture
async def operator_tool_names(gate_http, operator_jwt) -> set[str]:
    """Tool names visible to an operator JWT (decide + read scopes) via x-authentik-jwt."""
    transport = RemoteMCPServer(url=f"{gate_http}/mcp", headers={"x-authentik-jwt": operator_jwt}).to_transport()
    async with Client(transport) as client:
        tools = await client.list_tools()
    return {t.name for t in tools}


async def test_agent_cannot_see_operator_tools(agent_tool_names):
    """Agent JWT must not see approve_action/reject_action."""
    assert {"test_echo", "withdraw_action", "list_actions"} <= agent_tool_names
    assert {"approve_action", "reject_action"}.isdisjoint(agent_tool_names)


async def test_operator_jwt_sees_operator_tools(operator_tool_names):
    """Operator JWT must expose operator MCP tools."""
    assert {"approve_action", "reject_action", "list_actions"} <= operator_tool_names
    assert {"test_echo", "withdraw_action"}.isdisjoint(operator_tool_names)


if __name__ == "__main__":
    pytest_bazel.main()
