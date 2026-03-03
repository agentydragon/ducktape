"""Tests for MCP tool visibility with different auth credentials.

Verifies that an agent JWT (scopes: propose, read) cannot see operator-only
tools, while an operator JWT (scopes: decide, read) can. Uses a real HTTP
server to ensure FastMCP's auth enforcement works end-to-end.

Both roles authenticate via ``Authorization: Bearer`` JWTs verified against
the same JWKS endpoint by ``JWTVerifier``.
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

from approval_gate.conftest import GateAppFactory, agent_transport, operator_transport
from util.net import pick_free_port


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
async def gate_http(tmp_path, make_gate_app: GateAppFactory):
    """HTTP gate with an in-process FastMCP backend; yields base URL."""
    backend = FastMCP("test-backend")

    @backend.tool()
    async def echo(text: str) -> str:
        return f"echoed: {text}"

    app, _gate = make_gate_app(backend, tmp_path / "gate.db")
    gate_port = pick_free_port()
    async with _serve_app(app, port=gate_port):
        yield f"http://127.0.0.1:{gate_port}"


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
