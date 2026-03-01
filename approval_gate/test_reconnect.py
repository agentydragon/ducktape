"""Integration tests: client reconnection after approval gate server restart.

Verifies that a client can reconnect to the approval gate MCP server after the
server goes down and comes back up. Covers three scenarios:

1. Basic reconnection — new client connects after server restart, calls tools
2. Catch-up on reconnect — action resolved during outage, client reads terminal state
3. Re-subscribe for live notifications — re-subscribe to a pending action on a new
   connection, then approve it, verify ResourceUpdated notification is received

These tests use real HTTP servers (uvicorn) and real MCP clients to exercise the
full transport stack, simulating the pattern used by the OpenClaw plugin.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
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
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.mcp_config import RemoteMCPServer
from jwt import PyJWKClient
from mcp import types as mcp_types
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Mount

from approval_gate.mcp_auth import ApprovalGateAuthProvider
from approval_gate.models import Action, ActionStatus
from approval_gate.predicates import NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from mcp_infra.prefix import MCPMountPrefix
from util.net import pick_free_port

_AGENT_API_KEY = "test-agent-key"
_TEST_NS = MCPMountPrefix("test")


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
    jwks_client = PyJWKClient("http://test/jwks")
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

    async def wait_for(self, client: Client, action_id: uuid.UUID, status: ActionStatus) -> Action:
        """Wait until action reaches the given status via resource-updated notifications."""
        uri = f"resource://actions/{action_id}"
        while True:
            event = anyio.Event()
            self._events[uri] = event
            contents = await client.read_resource(uri)
            item = contents[0]
            assert isinstance(item, mcp_types.TextResourceContents)
            action = Action.model_validate_json(item.text)
            if action.state.status == status:
                self._events.pop(uri, None)
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
        async with _serve_app(app1, port=port), Client(_agent_transport(port)) as client:
            tools = await client.list_tools()
            assert any(t.name == "test_echo" for t in tools)

            result = await client.call_tool_mcp(
                "test_echo", {"input": {"text": "before-restart"}, "justification": "test"}
            )
            action_id_1 = uuid.UUID(json.loads(result.content[0].text))
            assert action_id_1 is not None

        # Server is now down — old client is disconnected

        # ── Phase 2: restart server on same port, same db ────────────────────
        app2, _gate2 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app2, port=port):
            # New client connects successfully
            async with Client(_agent_transport(port)) as client:
                tools = await client.list_tools()
                assert any(t.name == "test_echo" for t in tools)

                # Call tool again — new action
                result = await client.call_tool_mcp(
                    "test_echo", {"input": {"text": "after-restart"}, "justification": "test"}
                )
                action_id_2 = uuid.UUID(json.loads(result.content[0].text))
                assert action_id_2 != action_id_1

            # Approve via operator and verify execution
            async with Client(_operator_transport(port, admin_jwt)) as operator:
                await operator.call_tool("approve_action", {"action_id": str(action_id_2)})

            assert {"text": "after-restart"} in calls


async def test_pending_action_survives_server_restart(tmp_path, mock_jwks_signing_key, admin_jwt):
    """Action created before restart is readable and resolvable after restart."""
    port = pick_free_port()
    db_path = tmp_path / "gate.db"
    backend, calls = _make_backend()

    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=mock_jwks_signing_key):
        # ── Phase 1: create action ───────────────────────────────────────────
        app1, _gate1 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app1, port=port), Client(_agent_transport(port)) as client:
            result = await client.call_tool_mcp("test_echo", {"input": {"text": "survive"}, "justification": "test"})
            action_id = uuid.UUID(json.loads(result.content[0].text))

        # Server down — action is persisted in SQLite

        # ── Phase 2: restart, approve, verify catch-up ───────────────────────
        app2, _gate2 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app2, port=port):
            # Operator approves the action from before restart
            async with Client(_operator_transport(port, admin_jwt)) as operator:
                await operator.call_tool("approve_action", {"action_id": str(action_id)})

            # New agent client reads the action resource — should be done
            async with Client(_agent_transport(port)) as client:
                contents = await client.read_resource(f"resource://actions/{action_id}")
                item = contents[0]
                assert isinstance(item, mcp_types.TextResourceContents)
                action = Action.model_validate_json(item.text)
                assert action.state.status == ActionStatus.DONE

            assert {"text": "survive"} in calls


async def test_resubscribe_receives_notifications_after_restart(tmp_path, mock_jwks_signing_key, admin_jwt):
    """Re-subscribing to a pending action on a new connection receives notifications."""
    port = pick_free_port()
    db_path = tmp_path / "gate.db"
    backend, calls = _make_backend()

    with patch("jwt.PyJWKClient.get_signing_key_from_jwt", return_value=mock_jwks_signing_key):
        # ── Phase 1: create action ───────────────────────────────────────────
        app1, _gate1 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app1, port=port), Client(_agent_transport(port)) as client:
            result = await client.call_tool_mcp("test_echo", {"input": {"text": "resub"}, "justification": "test"})
            action_id = uuid.UUID(json.loads(result.content[0].text))

        # Server down

        # ── Phase 2: restart, re-subscribe, approve, wait for notification ───
        app2, _gate2 = _make_gate_app(backend, db_path, mock_jwks_signing_key)
        async with _serve_app(app2, port=port):
            waiter = _ResourceWaiter()
            async with Client(_agent_transport(port), message_handler=waiter) as agent:
                # Re-subscribe to the action from phase 1
                action_uri = AnyUrl(f"resource://actions/{action_id}")
                await agent.session.subscribe_resource(action_uri)

                # Approve via operator
                async with Client(_operator_transport(port, admin_jwt)) as operator:
                    await operator.call_tool("approve_action", {"action_id": str(action_id)})

                # Wait for the ResourceUpdated notification on the new connection
                with anyio.fail_after(10.0):
                    action = await waiter.wait_for(agent, action_id, ActionStatus.DONE)

                assert action.state.status == ActionStatus.DONE

            assert {"text": "resub"} in calls


if __name__ == "__main__":
    pytest_bazel.main()
