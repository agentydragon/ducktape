"""E2E tests for the Airlock over real HTTP transport.

Each test spins up a uvicorn server with auth, using agent and operator MCP
clients with proper Bearer tokens. This exercises the full transport stack
end-to-end.
"""

from __future__ import annotations

import anyio
import pytest
import pytest_bazel
from fastmcp import FastMCP
from starlette.applications import Starlette

from airlock.conftest import (
    TEST_NS,
    EchoBackend,
    GateAppFactory,
    GateClient,
    GateServerFactory,
    gate_http_app,
    serve_app,
)
from airlock.models import Action, ActionKey, ActionStatus, RejectedState, YieldAfterMs
from airlock.predicates import Approved, Denied
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import read_text


async def test_tool_list_wraps_backend_tools(echo_gate_app: Starlette, free_port: int, agent_client_transport: object):
    """MCP tool list exposes backend tools wrapped with the Airlock schema envelope."""
    async with serve_app(echo_gate_app, port=free_port), GateClient(agent_client_transport) as client:
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
    echo_gate_app: Starlette,
    free_port: int,
    agent_client_transport: object,
    operator_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """Happy path: tool call queued -> operator approves -> backend runs -> action done."""
    async with (
        serve_app(echo_gate_app, port=free_port),
        GateClient(agent_client_transport) as agent,
        GateClient(operator_client_transport) as operator,
    ):
        action = await agent.call_echo("hello", session_key=session_key)
        await operator.approve(action.key)
        with anyio.fail_after(5.0):
            await agent.wait_for(action.key, ActionStatus.DONE)

    assert echo_backend.calls == ["hello"]


async def test_reject_leaves_action_rejected_and_skips_backend(
    echo_gate_app: Starlette,
    free_port: int,
    agent_client_transport: object,
    operator_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """Reject path: tool call queued -> operator rejects -> rejected state, backend not called."""
    async with (
        serve_app(echo_gate_app, port=free_port),
        GateClient(agent_client_transport) as agent,
        GateClient(operator_client_transport) as operator,
    ):
        action = await agent.call_echo("no-run", session_key=session_key)
        await operator.reject(action.key, reason="test rejection")
        with anyio.fail_after(5.0):
            await agent.wait_for(action.key, ActionStatus.REJECTED)

    assert echo_backend.calls == []


async def test_auto_approve_predicate_skips_queue(
    make_gate_app: GateAppFactory,
    free_port: int,
    agent_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """Auto-approve predicate: tool call immediately executes without any operator action."""
    app = make_gate_app({TEST_NS: echo_backend.server}, predicate=lambda ns, tool, args: Approved())

    async with serve_app(app, port=free_port), GateClient(agent_client_transport) as agent:
        action = await agent.call_echo("auto", justification="auto", session_key=session_key)
        with anyio.fail_after(5.0):
            await agent.wait_for(action.key, ActionStatus.DONE)

    assert echo_backend.calls == ["auto"]


async def test_multi_backend_namespace_isolation(
    make_gate_app: GateAppFactory, free_port: int, agent_client_transport: object, session_key: str
):
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

    async with serve_app(app, port=free_port), GateClient(agent_client_transport) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert {"alpha_echo", "beta_echo"} <= tool_names

        action_a = await client.call_gate_tool(
            "alpha_echo", {"input": {"text": "from-a"}, "justification": "test", "session_key": session_key}
        )
        with anyio.fail_after(5.0):
            await client.wait_for(action_a.key, ActionStatus.DONE)

        action_b = await client.call_gate_tool(
            "beta_echo", {"input": {"text": "from-b"}, "justification": "test", "session_key": session_key}
        )
        with anyio.fail_after(5.0):
            await client.wait_for(action_b.key, ActionStatus.DONE)

    assert calls_a == ["from-a"]
    assert calls_b == ["from-b"]


async def test_action_seq_increments_within_session(
    echo_gate_app: Starlette, free_port: int, agent_client_transport: object, session_key: str
):
    """Action sequences increment monotonically within a session."""
    async with serve_app(echo_gate_app, port=free_port), GateClient(agent_client_transport) as client:
        a1 = await client.call_echo("a", justification="t", session_key=session_key)
        a2 = await client.call_echo("b", justification="t", session_key=session_key)

    assert a1.key.session_key == session_key
    assert a2.key.session_key == session_key
    assert a1.key.action_seq == 1
    assert a2.key.action_seq == 2


async def test_log_hwm_increments_on_state_changes(
    echo_gate_app: Starlette,
    free_port: int,
    agent_client_transport: object,
    operator_client_transport: object,
    session_key: str,
):
    """The session log HWM increases as actions are received and decided."""
    async with (
        serve_app(echo_gate_app, port=free_port),
        GateClient(agent_client_transport) as agent,
        GateClient(operator_client_transport) as operator,
    ):
        action = await agent.call_echo("log-test", justification="t", session_key=session_key)

        hwm_after_create = int(await read_text(agent, f"resource://sessions/{session_key}/log_hwm"))
        assert hwm_after_create >= 1

        await operator.approve(action.key)
        with anyio.fail_after(5.0):
            await agent.wait_for(action.key, ActionStatus.DONE)

        hwm_after_done = int(await read_text(agent, f"resource://sessions/{session_key}/log_hwm"))
        assert hwm_after_done > hwm_after_create


def _auto_approve(ns: str, tool: str, args: dict) -> Approved:
    return Approved()


@pytest.mark.parametrize(
    ("server_kwargs", "call_kwargs", "expected_status"),
    [
        pytest.param(
            {"predicate": _auto_approve, "default_wait_mode": YieldAfterMs(timeout_ms=5000)},
            {},
            ActionStatus.DONE,
            id="server-default-yield-done",
        ),
        pytest.param(
            {"predicate": _auto_approve},
            {"wait_mode": {"mode": "yield_after_ms", "timeout_ms": 5000}},
            ActionStatus.DONE,
            id="per-call-yield-done",
        ),
        pytest.param(
            {"predicate": _auto_approve}, {"wait_mode": {"mode": "blocking"}}, ActionStatus.DONE, id="blocking-done"
        ),
        pytest.param({}, {}, ActionStatus.PENDING, id="no-wait-pending"),
        pytest.param(
            {},
            {"wait_mode": {"mode": "yield_after_ms", "timeout_ms": 100}},
            ActionStatus.PENDING,
            id="short-yield-pending",
        ),
    ],
)
async def test_wait_mode_resolution(
    make_gate_server: GateServerFactory,
    free_port: int,
    agent_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
    server_kwargs: dict,
    call_kwargs: dict,
    expected_status: ActionStatus,
):
    """Various wait_mode / server-default combinations resolve to expected status."""
    gate = make_gate_server({TEST_NS: echo_backend.server}, **server_kwargs)
    app = gate_http_app(gate)

    async with serve_app(app, port=free_port), GateClient(agent_client_transport) as agent:
        with anyio.fail_after(10.0):
            action = await agent.call_echo("test", session_key=session_key, **call_kwargs)

    assert action.state.status == expected_status


async def test_auto_deny_with_timeout_returns_rejected(
    make_gate_server: GateServerFactory,
    free_port: int,
    agent_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """Auto-deny predicate + server timeout -> rejected with reason."""
    gate = make_gate_server(
        {TEST_NS: echo_backend.server},
        predicate=lambda ns, tool, args: Denied(reason="nope"),
        default_wait_mode=YieldAfterMs(timeout_ms=5000),
    )
    app = gate_http_app(gate)

    async with serve_app(app, port=free_port), GateClient(agent_client_transport) as agent:
        action = await agent.call_echo("deny-me", session_key=session_key)

    assert action.state.status == ActionStatus.REJECTED
    assert isinstance(action.state, RejectedState)
    assert action.state.reason == "nope"
    assert echo_backend.calls == []


# ── per-call wait_mode overrides server default ──────────────────────────


async def test_yield_zero_overrides_large_server_default(
    make_gate_server: GateServerFactory,
    free_port: int,
    agent_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """yield_after_ms=0 returns immediately despite a 30s server default."""
    gate = make_gate_server(
        {TEST_NS: echo_backend.server}, predicate=_auto_approve, default_wait_mode=YieldAfterMs(timeout_ms=30000)
    )
    app = gate_http_app(gate)

    async with serve_app(app, port=free_port), GateClient(agent_client_transport) as agent:
        with anyio.fail_after(5.0):
            action = await agent.call_echo(
                "bg", session_key=session_key, wait_mode={"mode": "yield_after_ms", "timeout_ms": 0}
            )

    # Returns before auto-approve completes — any status is valid
    assert action.state.status in (ActionStatus.PENDING, ActionStatus.EXECUTING, ActionStatus.DONE)


async def test_blocking_overrides_tiny_server_default(
    make_gate_server: GateServerFactory,
    free_port: int,
    agent_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """blocking wait_mode overrides a 10ms server default — waits for completion."""
    gate = make_gate_server(
        {TEST_NS: echo_backend.server}, predicate=_auto_approve, default_wait_mode=YieldAfterMs(timeout_ms=10)
    )
    app = gate_http_app(gate)

    async with serve_app(app, port=free_port), GateClient(agent_client_transport) as agent:
        with anyio.fail_after(10.0):
            action = await agent.call_echo("override", session_key=session_key, wait_mode={"mode": "blocking"})

    assert action.state.status == ActionStatus.DONE
    assert echo_backend.calls == ["override"]


# ── blocking with operator ───────────────────────────────────────────────


async def test_blocking_waits_for_operator_approval(
    make_gate_server: GateServerFactory,
    free_port: int,
    agent_client_transport: object,
    operator_client_transport: object,
    echo_backend: EchoBackend,
    session_key: str,
):
    """blocking wait_mode with NeedsHumanDecision blocks until operator approves."""
    gate = make_gate_server({TEST_NS: echo_backend.server})
    app = gate_http_app(gate)

    async with (
        serve_app(app, port=free_port),
        GateClient(agent_client_transport) as agent,
        GateClient(operator_client_transport) as operator,
    ):
        async with anyio.create_task_group() as tg:
            action_holder: list[Action] = []

            async def blocking_call():
                action = await agent.call_echo("block-human", session_key=session_key, wait_mode={"mode": "blocking"})
                action_holder.append(action)

            tg.start_soon(blocking_call)

            await anyio.sleep(0.5)
            key = ActionKey(session_key=session_key, action_seq=1)
            await operator.approve(key)

        assert len(action_holder) == 1
        assert action_holder[0].state.status == ActionStatus.DONE
        assert echo_backend.calls == ["block-human"]


if __name__ == "__main__":
    pytest_bazel.main()
