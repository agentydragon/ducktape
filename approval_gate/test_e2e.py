"""E2E tests for the approval gate over real HTTP transport.

Each test spins up a uvicorn server with JWT authentication, using agent and
operator MCP clients with proper Bearer tokens. This exercises the full auth
and transport stack end-to-end.
"""

from __future__ import annotations

import anyio
import pytest_bazel
from fastmcp import FastMCP

from approval_gate.conftest import TEST_NS, GateAppFactory, GateClient, agent_transport, operator_transport, serve_app
from approval_gate.models import ActionStatus
from approval_gate.predicates import Approved
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import read_text

_SESSION = "e2e-session"


def _make_backend():
    calls: list[str] = []
    srv = FastMCP()

    @srv.tool()
    async def echo(text: str) -> str:
        calls.append(text)
        return f"echoed: {text}"

    return srv, calls


async def test_tool_list_wraps_backend_tools(make_gate_app: GateAppFactory, free_port: int, agent_jwt: str):
    """MCP tool list exposes backend tools wrapped with the approval-gate schema envelope."""
    backend, _ = _make_backend()
    app = make_gate_app({TEST_NS: backend})
    base_url = f"http://127.0.0.1:{free_port}"

    async with serve_app(app, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as client:
        tools = await client.list_tools()

    names = [t.name for t in tools]
    assert "test_echo" in names

    echo = next(t for t in tools if t.name == "test_echo")
    props = echo.inputSchema["properties"]
    assert "justification" in props
    assert "session_key" in props
    assert "input" in props
    assert "text" in props["input"]["properties"]


async def test_approve_executes_backend_tool(
    make_gate_app: GateAppFactory, free_port: int, agent_jwt: str, operator_jwt: str
):
    """Happy path: tool call queued -> operator approves -> backend runs -> action done."""
    backend, calls = _make_backend()
    app = make_gate_app({TEST_NS: backend})
    base_url = f"http://127.0.0.1:{free_port}"

    async with (
        serve_app(app, port=free_port),
        GateClient(agent_transport(base_url, agent_jwt)) as agent,
        GateClient(operator_transport(base_url, operator_jwt)) as operator,
    ):
        key = await agent.call_echo("hello", session_key=_SESSION)
        await operator.approve(key)
        with anyio.fail_after(5.0):
            await agent.wait_for(key, ActionStatus.DONE)

    assert calls == ["hello"]


async def test_reject_leaves_action_rejected_and_skips_backend(
    make_gate_app: GateAppFactory, free_port: int, agent_jwt: str, operator_jwt: str
):
    """Reject path: tool call queued -> operator rejects -> rejected state, backend not called."""
    backend, calls = _make_backend()
    app = make_gate_app({TEST_NS: backend})
    base_url = f"http://127.0.0.1:{free_port}"

    async with (
        serve_app(app, port=free_port),
        GateClient(agent_transport(base_url, agent_jwt)) as agent,
        GateClient(operator_transport(base_url, operator_jwt)) as operator,
    ):
        key = await agent.call_echo("no-run", session_key=_SESSION)
        await operator.reject(key, reason="test rejection")
        with anyio.fail_after(5.0):
            await agent.wait_for(key, ActionStatus.REJECTED)

    assert calls == []


async def test_auto_approve_predicate_skips_queue(make_gate_app: GateAppFactory, free_port: int, agent_jwt: str):
    """Auto-approve predicate: tool call immediately executes without any operator action."""
    backend, calls = _make_backend()
    app = make_gate_app({TEST_NS: backend}, predicate=lambda ns, tool, args: Approved())
    base_url = f"http://127.0.0.1:{free_port}"

    async with serve_app(app, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as agent:
        key = await agent.call_echo("auto", justification="auto", session_key=_SESSION)
        with anyio.fail_after(5.0):
            await agent.wait_for(key, ActionStatus.DONE)

    assert calls == ["auto"]


async def test_multi_backend_namespace_isolation(make_gate_app: GateAppFactory, free_port: int, agent_jwt: str):
    """Multiple backends each get namespaced tools that route to the correct backend."""
    calls_a: list[str] = []
    calls_b: list[str] = []

    srv_a = FastMCP()

    @srv_a.tool()
    async def echo(text: str) -> str:
        calls_a.append(text)
        return f"a: {text}"

    srv_b = FastMCP()

    @srv_b.tool(name="echo")
    async def echo_b(text: str) -> str:
        calls_b.append(text)
        return f"b: {text}"

    ns_a = MCPMountPrefix("alpha")
    ns_b = MCPMountPrefix("beta")
    app = make_gate_app({ns_a: srv_a, ns_b: srv_b}, predicate=lambda ns, tool, args: Approved())
    base_url = f"http://127.0.0.1:{free_port}"

    async with serve_app(app, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert {"alpha_echo", "beta_echo"} <= tool_names

        key_a = await client.call_gate_tool(
            "alpha_echo", {"input": {"text": "from-a"}, "justification": "test", "session_key": _SESSION}
        )
        with anyio.fail_after(5.0):
            await client.wait_for(key_a, ActionStatus.DONE)

        key_b = await client.call_gate_tool(
            "beta_echo", {"input": {"text": "from-b"}, "justification": "test", "session_key": _SESSION}
        )
        with anyio.fail_after(5.0):
            await client.wait_for(key_b, ActionStatus.DONE)

    assert calls_a == ["from-a"]
    assert calls_b == ["from-b"]


async def test_action_seq_increments_within_session(make_gate_app: GateAppFactory, free_port: int, agent_jwt: str):
    """Action sequences increment monotonically within a session."""
    backend, _ = _make_backend()
    app = make_gate_app({TEST_NS: backend})
    base_url = f"http://127.0.0.1:{free_port}"

    async with serve_app(app, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as client:
        k1 = await client.call_echo("a", justification="t", session_key=_SESSION)
        k2 = await client.call_echo("b", justification="t", session_key=_SESSION)

    assert k1.session_key == _SESSION
    assert k2.session_key == _SESSION
    assert k1.action_seq == 1
    assert k2.action_seq == 2


async def test_log_hwm_increments_on_state_changes(
    make_gate_app: GateAppFactory, free_port: int, agent_jwt: str, operator_jwt: str
):
    """The session log HWM increases as actions are received and decided."""
    backend, _ = _make_backend()
    app = make_gate_app({TEST_NS: backend})
    base_url = f"http://127.0.0.1:{free_port}"

    async with (
        serve_app(app, port=free_port),
        GateClient(agent_transport(base_url, agent_jwt)) as agent,
        GateClient(operator_transport(base_url, operator_jwt)) as operator,
    ):
        key = await agent.call_echo("log-test", justification="t", session_key=_SESSION)

        hwm_after_create = int(await read_text(agent, f"resource://sessions/{_SESSION}/log_hwm"))
        assert hwm_after_create >= 1

        await operator.approve(key)
        with anyio.fail_after(5.0):
            await agent.wait_for(key, ActionStatus.DONE)

        hwm_after_done = int(await read_text(agent, f"resource://sessions/{_SESSION}/log_hwm"))
        assert hwm_after_done > hwm_after_create


if __name__ == "__main__":
    pytest_bazel.main()
