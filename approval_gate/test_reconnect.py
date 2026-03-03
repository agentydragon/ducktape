"""Integration tests: client reconnection after approval gate server restart.

Verifies that a client can reconnect to the approval gate MCP server after the
server goes down and comes back up. Covers three scenarios:

1. Basic reconnection — new client connects after server restart, calls tools
2. Catch-up on reconnect — action resolved during outage, client reads terminal state
3. Re-subscribe for live notifications — re-subscribe to a session's log HWM on a new
   connection, then approve an action, verify ResourceUpdated notification is received

These tests use real HTTP servers (uvicorn) and real MCP clients to exercise the
full transport stack, simulating the pattern used by the OpenClaw plugin.
"""

from __future__ import annotations

import anyio
import pytest_bazel
from fastmcp import FastMCP

from approval_gate.conftest import TEST_NS, GateAppFactory, GateClient, agent_transport, operator_transport, serve_app
from approval_gate.models import Action, ActionStatus
from mcp_infra.resource_utils import read_text_json_typed

_SESSION = "reconnect-session"


def _make_backend():
    calls: list[str] = []
    backend = FastMCP()

    @backend.tool()
    async def echo(text: str) -> str:
        calls.append(text)
        return f"echoed: {text}"

    return backend, calls


async def test_client_reconnects_after_server_restart(
    make_gate_app: GateAppFactory, free_port: int, agent_jwt: str, operator_jwt: str
):
    """New client connects after server restart and can call tools successfully."""
    backend, calls = _make_backend()
    base_url = f"http://127.0.0.1:{free_port}"

    # ── Phase 1: start server, call tool ─────────────────────────────────
    app1 = make_gate_app({TEST_NS: backend})
    async with serve_app(app1, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as agent:
        tools = await agent.list_tools()
        assert any(t.name == "test_echo" for t in tools)

        key_1 = await agent.call_echo("before-restart", session_key=_SESSION)
        assert key_1.session_key == _SESSION

    # Server is now down — old client is disconnected

    # ── Phase 2: restart server on same port, same db ────────────────────
    app2 = make_gate_app({TEST_NS: backend})
    async with serve_app(app2, port=free_port):
        # New client connects successfully
        async with GateClient(agent_transport(base_url, agent_jwt)) as agent:
            tools = await agent.list_tools()
            assert any(t.name == "test_echo" for t in tools)

            # Call tool again — new action
            key_2 = await agent.call_echo("after-restart", session_key=_SESSION)
            assert key_2.action_seq > key_1.action_seq

        # Approve via operator and verify execution
        async with GateClient(operator_transport(base_url, operator_jwt)) as operator:
            await operator.approve(key_2)

        assert "after-restart" in calls


async def test_pending_action_survives_server_restart(
    make_gate_app: GateAppFactory, free_port: int, agent_jwt: str, operator_jwt: str
):
    """Action created before restart is readable and resolvable after restart."""
    backend, calls = _make_backend()
    base_url = f"http://127.0.0.1:{free_port}"

    # ── Phase 1: create action ───────────────────────────────────────────
    app1 = make_gate_app({TEST_NS: backend})
    async with serve_app(app1, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as agent:
        key = await agent.call_echo("survive", session_key=_SESSION)

    # Server down — action is persisted in SQLite

    # ── Phase 2: restart, approve, verify catch-up ───────────────────────
    app2 = make_gate_app({TEST_NS: backend})
    async with (
        serve_app(app2, port=free_port),
        GateClient(operator_transport(base_url, operator_jwt)) as operator,
        GateClient(agent_transport(base_url, agent_jwt)) as agent,
    ):
        await operator.approve(key)
        action_uri = f"resource://sessions/{key.session_key}/actions/{key.action_seq}"
        action: Action = await read_text_json_typed(agent, action_uri, Action)
        assert action.state.status == ActionStatus.DONE

    assert "survive" in calls


async def test_resubscribe_receives_notifications_after_restart(
    make_gate_app: GateAppFactory, free_port: int, agent_jwt: str, operator_jwt: str
):
    """Re-subscribing to a session's log HWM on a new connection receives notifications."""
    backend, calls = _make_backend()
    base_url = f"http://127.0.0.1:{free_port}"

    # ── Phase 1: create action ───────────────────────────────────────────
    app1 = make_gate_app({TEST_NS: backend})
    async with serve_app(app1, port=free_port), GateClient(agent_transport(base_url, agent_jwt)) as agent:
        key = await agent.call_echo("resub", session_key=_SESSION)

    # Server down

    # ── Phase 2: restart, re-subscribe, approve, wait for notification ───
    app2 = make_gate_app({TEST_NS: backend})
    async with serve_app(app2, port=free_port):
        async with GateClient(agent_transport(base_url, agent_jwt)) as agent:
            # Approve via operator
            async with GateClient(operator_transport(base_url, operator_jwt)) as operator:
                await operator.approve(key)

            # Wait for the ResourceUpdated notification on the new connection
            with anyio.fail_after(10.0):
                action = await agent.wait_for(key, ActionStatus.DONE)

            assert action.state.status == ActionStatus.DONE

        assert "resub" in calls


if __name__ == "__main__":
    pytest_bazel.main()
