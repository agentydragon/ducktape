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

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import jwt as pyjwt
import pytest
import pytest_bazel
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import FastMCP
from fastmcp.client.messages import MessageHandler
from fastmcp.mcp_config import RemoteMCPServer
from mcp import types as mcp_types
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Mount

from approval_gate.conftest import GateClient
from approval_gate.mcp_auth import ApprovalGateAuthProvider
from approval_gate.models import Action, ActionKey, ActionStatus
from approval_gate.predicates import NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import read_text_json_typed
from util.net import pick_free_port

_AGENT_API_KEY = "test-agent-key"
_TEST_NS = MCPMountPrefix("test")
_SESSION = "reconnect-session"


@asynccontextmanager
async def _serve_app(app, *, port: int):
    """Start a uvicorn server in a dedicated thread; yield when ready; shut down on exit."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread exited before starting")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=3.0)
            raise TimeoutError(f"server did not start on port {port}")
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=3.0)


def _make_backend():
    """Create a simple FastMCP backend with an echo tool."""
    calls: list[dict] = []
    backend = FastMCP("test-backend")

    @backend.tool()
    async def echo(text: str) -> str:
        calls.append({"text": text})
        return f"echoed: {text}"

    return backend, calls


def _make_gate_app(backend: FastMCP, db_path: Path, mock_jwks_signing_key):
    """Create a Starlette app with an ApprovalGateServer."""
    jwks_client = pyjwt.PyJWKClient("http://test/jwks")
    auth = ApprovalGateAuthProvider(agent_api_key=_AGENT_API_KEY, jwks_client=jwks_client)
    gate = ApprovalGateServer(
        backends={_TEST_NS: backend},
        db_path=db_path,
        predicate=lambda ns, tool, args: NeedsHumanDecision(),
        public_base_url="http://test",
        auth=auth,
    )
    mcp_app = gate.http_app(path="/")
    app = Starlette(routes=[Mount("/mcp", app=mcp_app)], lifespan=mcp_app.lifespan)
    return app, gate


def _agent_transport(port: int):
    """Create an agent-scoped MCP client transport."""
    return RemoteMCPServer(
        url=f"http://127.0.0.1:{port}/mcp", headers={"Authorization": f"Bearer {_AGENT_API_KEY}"}
    ).to_transport()


def _operator_transport(port: int, admin_jwt: str):
    """Create an operator-scoped MCP client transport."""
    return RemoteMCPServer(url=f"http://127.0.0.1:{port}/mcp", headers={"x-authentik-jwt": admin_jwt}).to_transport()


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


class _ResourceWaiter(MessageHandler):
    """Receives resource-updated notifications and signals waiters."""

    def __init__(self) -> None:
        self._events: dict[str, anyio.Event] = {}

    async def on_resource_updated(self, notification: mcp_types.ResourceUpdatedNotification) -> None:
        uri = str(notification.params.uri)
        evt = self._events.get(uri)
        if evt is not None:
            evt.set()

    async def wait_for(self, client: GateClient, key: ActionKey, status: ActionStatus) -> Action:
        """Wait until action reaches the given status via resource-updated notifications."""
        action_uri = f"resource://sessions/{key.session_key}/actions/{key.action_seq}"
        hwm_uri = f"resource://sessions/{key.session_key}/log_hwm"
        # Subscribe so the server sends notifications to this session
        await client.session.subscribe_resource(AnyUrl(action_uri))
        await client.session.subscribe_resource(AnyUrl(hwm_uri))
        while True:
            event = anyio.Event()
            self._events[hwm_uri] = event
            self._events[action_uri] = event
            action: Action = await read_text_json_typed(client, action_uri, Action)
            if action.state.status == status:
                self._events.pop(hwm_uri, None)
                self._events.pop(action_uri, None)
                return action
            await event.wait()


async def test_client_reconnects_after_server_restart(tmp_path, mock_jwks_signing_key, admin_jwt):
    """New client connects after server restart and can call tools successfully."""
    port = pick_free_port()
    db_path = tmp_path / "gate.db"
    backend, calls = _make_backend()

    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=mock_jwks_signing_key):
        # ── Phase 1: start server, call tool ─────────────────────────────────
        app1, _gate1 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app1, port=port), GateClient(_agent_transport(port)) as agent:
            tools = await agent.list_tools()
            assert any(t.name == "test_echo" for t in tools)

            key_1 = await agent.call_echo("before-restart", session_key=_SESSION)
            assert key_1.session_key == _SESSION

        # Server is now down — old client is disconnected

        # ── Phase 2: restart server on same port, same db ────────────────────
        app2, _gate2 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app2, port=port):
            # New client connects successfully
            async with GateClient(_agent_transport(port)) as agent:
                tools = await agent.list_tools()
                assert any(t.name == "test_echo" for t in tools)

                # Call tool again — new action
                key_2 = await agent.call_echo("after-restart", session_key=_SESSION)
                assert key_2.action_seq > key_1.action_seq

            # Approve via operator and verify execution
            async with GateClient(_operator_transport(port, admin_jwt)) as operator:
                await operator.approve(key_2)

            assert {"text": "after-restart"} in calls


async def test_pending_action_survives_server_restart(tmp_path, mock_jwks_signing_key, admin_jwt):
    """Action created before restart is readable and resolvable after restart."""
    port = pick_free_port()
    db_path = tmp_path / "gate.db"
    backend, calls = _make_backend()

    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=mock_jwks_signing_key):
        # ── Phase 1: create action ───────────────────────────────────────────
        app1, _gate1 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app1, port=port), GateClient(_agent_transport(port)) as agent:
            key = await agent.call_echo("survive", session_key=_SESSION)

        # Server down — action is persisted in SQLite

        # ── Phase 2: restart, approve, verify catch-up ───────────────────────
        app2, _gate2 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app2, port=port):
            # Operator approves the action from before restart
            async with GateClient(_operator_transport(port, admin_jwt)) as operator:
                await operator.approve(key)

            # New agent client reads the action resource — should be done
            async with GateClient(_agent_transport(port)) as agent:
                action_uri = f"resource://sessions/{key.session_key}/actions/{key.action_seq}"
                action: Action = await read_text_json_typed(agent, action_uri, Action)
                assert action.state.status == ActionStatus.DONE

            assert {"text": "survive"} in calls


async def test_resubscribe_receives_notifications_after_restart(tmp_path, mock_jwks_signing_key, admin_jwt):
    """Re-subscribing to a session's log HWM on a new connection receives notifications."""
    port = pick_free_port()
    db_path = tmp_path / "gate.db"
    backend, calls = _make_backend()

    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=mock_jwks_signing_key):
        # ── Phase 1: create action ───────────────────────────────────────────
        app1, _gate1 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app1, port=port), GateClient(_agent_transport(port)) as agent:
            key = await agent.call_echo("resub", session_key=_SESSION)

        # Server down

        # ── Phase 2: restart, re-subscribe, approve, wait for notification ───
        app2, _gate2 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app2, port=port):
            waiter = _ResourceWaiter()
            async with GateClient(_agent_transport(port), message_handler=waiter) as agent:
                # Re-subscribe to the session's log HWM from phase 1
                hwm_uri = AnyUrl(f"resource://sessions/{key.session_key}/log_hwm")
                await agent.session.subscribe_resource(hwm_uri)

                # Approve via operator
                async with GateClient(_operator_transport(port, admin_jwt)) as operator:
                    await operator.approve(key)

                # Wait for the ResourceUpdated notification on the new connection
                with anyio.fail_after(10.0):
                    action = await waiter.wait_for(agent, key, ActionStatus.DONE)

                assert action.state.status == ActionStatus.DONE

            assert {"text": "resub"} in calls


if __name__ == "__main__":
    pytest_bazel.main()
