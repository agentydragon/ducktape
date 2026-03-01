"""E2E tests for the approval gate — fully in-process, no HTTP servers.

FastMCP only skips auth checks for STDIO transport; the in-process memory
transport enforces auth. The _agent_auth_ctx autouse fixture injects an
agent-scoped access token via the MCP auth_context_var before the server
task is started (anyio copies contextvars to new tasks), allowing the
in-process client to call agent-scoped tools.

Operator actions (approve/reject) are called via gate.decide() directly
because in-process clients only have AGENT_SCOPE, not OPERATOR_SCOPE.
The full MCP auth boundary is covered by test_operator_auth.py.
"""

from __future__ import annotations

import anyio
import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client.messages import MessageHandler
from mcp import types as mcp_types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken as MCPAccessToken
from pydantic import AnyUrl

from approval_gate.conftest import GateClient
from approval_gate.mcp_auth import AGENT_SCOPE, READER_SCOPE
from approval_gate.models import Action, ActionKey, ActionStatus, ApproveDecision, DenyDecision
from approval_gate.predicates import Approved, NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.resource_utils import read_text, read_text_json_typed

_TEST_NS = MCPMountPrefix("test")
_SESSION = "e2e-session"


@pytest.fixture(autouse=True)
async def _agent_auth_ctx():
    """Inject AGENT_SCOPE + READER_SCOPE into the MCP auth context for in-process tests."""
    user = AuthenticatedUser(MCPAccessToken(token="test-agent", client_id="test", scopes=[AGENT_SCOPE, READER_SCOPE]))
    token = auth_context_var.set(user)
    yield
    auth_context_var.reset(token)


@pytest.fixture
async def backend():
    calls: list[dict] = []
    srv = FastMCP("test-backend")

    @srv.tool()
    async def echo(text: str) -> str:
        calls.append({"text": text})
        return f"echoed: {text}"

    return srv, calls


def _make_gate(srv, tmp_path, predicate, db_name="gate.db"):
    return ApprovalGateServer(
        backends={_TEST_NS: srv}, db_path=tmp_path / db_name, predicate=predicate, public_base_url="http://test"
    )


@pytest.fixture
async def gate(backend, tmp_path):
    srv, _ = backend
    return _make_gate(srv, tmp_path, lambda ns, tool, args: NeedsHumanDecision())


class _ResourceWaiter(MessageHandler):
    """Receives resource-updated notifications and signals waiters.

    Subscribes to the session log HWM and action resources. On each notification,
    reads the action resource to check if the target status has been reached.
    """

    def __init__(self) -> None:
        self._events: dict[str, anyio.Event] = {}

    async def on_resource_updated(self, notification: mcp_types.ResourceUpdatedNotification) -> None:
        uri = str(notification.params.uri)
        evt = self._events.get(uri)
        if evt is not None:
            evt.set()

    async def wait_for(self, client: GateClient, key: ActionKey, status: ActionStatus) -> Action:
        """Wait until the action reaches `status` via resource-updated notifications."""
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


async def test_tool_list_wraps_backend_tools(gate):
    """MCP tool list exposes backend tools wrapped with the approval-gate schema envelope."""
    async with GateClient(gate) as client:
        tools = await client.list_tools()

    names = [t.name for t in tools]
    assert "test_echo" in names

    echo = next(t for t in tools if t.name == "test_echo")
    props = echo.inputSchema["properties"]
    assert "justification" in props
    assert "session_key" in props
    assert "input" in props
    assert "text" in props["input"]["properties"]


async def test_approve_executes_backend_tool(gate, backend):
    """Happy path: tool call queued -> operator approves -> backend runs -> action done."""
    _, calls = backend
    waiter = _ResourceWaiter()
    async with GateClient(gate, message_handler=waiter) as client:
        key = await client.call_echo("hello", session_key=_SESSION)
        await gate.decide(key, ApproveDecision())
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, key, ActionStatus.DONE)
    assert calls == [{"text": "hello"}]


async def test_reject_leaves_action_rejected_and_skips_backend(gate, backend):
    """Reject path: tool call queued -> operator rejects -> rejected state, backend not called."""
    _, calls = backend
    waiter = _ResourceWaiter()
    async with GateClient(gate, message_handler=waiter) as client:
        key = await client.call_echo("no-run", session_key=_SESSION)
        await gate.decide(key, DenyDecision(reason="test rejection"))
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, key, ActionStatus.REJECTED)
    assert calls == []


async def test_auto_approve_predicate_skips_queue(backend, tmp_path):
    """Auto-approve predicate: tool call immediately executes without any operator action."""
    srv, calls = backend
    gate = _make_gate(srv, tmp_path, lambda ns, tool, args: Approved(), db_name="gate_auto.db")
    waiter = _ResourceWaiter()
    async with GateClient(gate, message_handler=waiter) as client:
        key = await client.call_echo("auto", justification="auto", session_key=_SESSION)
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, key, ActionStatus.DONE)
    assert calls == [{"text": "auto"}]


async def test_multi_backend_namespace_isolation(tmp_path):
    """Multiple backends each get namespaced tools that route to the correct backend."""
    calls_a: list[dict] = []
    calls_b: list[dict] = []

    srv_a = FastMCP("backend-a")

    @srv_a.tool()
    async def echo(text: str) -> str:
        calls_a.append({"text": text})
        return f"a: {text}"

    srv_b = FastMCP("backend-b")

    @srv_b.tool(name="echo")
    async def echo_b(text: str) -> str:
        calls_b.append({"text": text})
        return f"b: {text}"

    ns_a = MCPMountPrefix("alpha")
    ns_b = MCPMountPrefix("beta")
    gate = ApprovalGateServer(
        backends={ns_a: srv_a, ns_b: srv_b},
        db_path=tmp_path / "gate_multi.db",
        predicate=lambda ns, tool, args: Approved(),
        public_base_url="http://test",
    )
    waiter = _ResourceWaiter()
    async with GateClient(gate, message_handler=waiter) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "alpha_echo" in tool_names
        assert "beta_echo" in tool_names

        key_a = await client.call_gate_tool(
            "alpha_echo", {"input": {"text": "from-a"}, "justification": "test", "session_key": _SESSION}
        )
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, key_a, ActionStatus.DONE)

        key_b = await client.call_gate_tool(
            "beta_echo", {"input": {"text": "from-b"}, "justification": "test", "session_key": _SESSION}
        )
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, key_b, ActionStatus.DONE)

    assert calls_a == [{"text": "from-a"}]
    assert calls_b == [{"text": "from-b"}]


async def test_action_seq_increments_within_session(gate, backend):
    """Action sequences increment monotonically within a session."""
    async with GateClient(gate) as client:
        k1 = await client.call_echo("a", justification="t", session_key=_SESSION)
        k2 = await client.call_echo("b", justification="t", session_key=_SESSION)
    assert k1.session_key == _SESSION
    assert k2.session_key == _SESSION
    assert k1.action_seq == 1
    assert k2.action_seq == 2


async def test_log_hwm_increments_on_state_changes(gate, backend):
    """The session log HWM increases as actions are received and decided."""
    waiter = _ResourceWaiter()
    async with GateClient(gate, message_handler=waiter) as client:
        key = await client.call_echo("log-test", justification="t", session_key=_SESSION)

        # After creation, HWM should be at least 1 (ACTION_RECEIVED)
        hwm_after_create = int(await read_text(client, f"resource://sessions/{_SESSION}/log_hwm"))
        assert hwm_after_create >= 1

        # Approve and wait for done
        await gate.decide(key, ApproveDecision())
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, key, ActionStatus.DONE)

        # HWM should have advanced
        hwm_after_done = int(await read_text(client, f"resource://sessions/{_SESSION}/log_hwm"))
        assert hwm_after_done > hwm_after_create


if __name__ == "__main__":
    pytest_bazel.main()
