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

import json
import uuid

import anyio
import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from mcp import types as mcp_types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken as MCPAccessToken

from approval_gate.mcp_auth import AGENT_SCOPE
from approval_gate.models import Action, ActionStatus, ApproveDecision, DenyDecision
from approval_gate.predicates import Approved, NeedsHumanDecision
from approval_gate.proxy_server import ApprovalGateServer
from mcp_infra.prefix import MCPMountPrefix

_TEST_NS = MCPMountPrefix("test")


@pytest.fixture(autouse=True)
async def _agent_auth_ctx():
    """Inject AGENT_SCOPE into the MCP auth context for in-process tests.

    Sets auth_context_var before the server task is created so the server task
    inherits agent scope (anyio copies contextvars to new asyncio tasks).
    """
    user = AuthenticatedUser(MCPAccessToken(token="test-agent", client_id="test", scopes=[AGENT_SCOPE]))
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

    Calling read_resource from within on_resource_updated would deadlock because
    on_resource_updated is dispatched by the client's _receive_loop, which cannot
    process its own response. Instead we just signal the event here and let
    wait_for() do the resource read from outside _receive_loop.
    """

    def __init__(self) -> None:
        self._events: dict[str, anyio.Event] = {}

    async def on_resource_updated(self, notification: mcp_types.ResourceUpdatedNotification) -> None:
        uri = str(notification.params.uri)
        evt = self._events.get(uri)
        if evt is not None:
            evt.set()

    async def wait_for(self, client: Client, action_id: uuid.UUID, status: ActionStatus) -> Action:
        """Wait until `action_id` reaches `status` via resource-updated notifications.

        Sets up the event before reading the resource so no notification is missed
        regardless of when it arrives relative to the read.
        """
        uri = f"resource://actions/{action_id}"
        while True:
            # Register event before reading so we catch notifications that arrive
            # concurrently with the read.
            event = anyio.Event()
            self._events[uri] = event
            contents = await client.read_resource(uri)
            item = contents[0]
            assert isinstance(item, mcp_types.TextResourceContents)
            action = Action.model_validate_json(item.text)
            if action.state.status == status:
                self._events.pop(uri, None)
                return action
            # Wait for next notification, then loop to re-read.
            await event.wait()


async def test_tool_list_wraps_backend_tools(gate):
    """MCP tool list exposes backend tools wrapped with the approval-gate schema envelope."""
    async with Client(gate) as client:
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
    """Happy path: tool call queued → operator approves → backend runs → action done."""
    _, calls = backend
    waiter = _ResourceWaiter()
    async with Client(gate, message_handler=waiter) as client:
        result = await client.call_tool_mcp("test_echo", {"input": {"text": "hello"}, "justification": "test"})
        action_id = uuid.UUID(json.loads(result.content[0].text))
        await gate.decide(action_id, ApproveDecision())
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, action_id, ActionStatus.DONE)
    assert calls == [{"text": "hello"}]


async def test_reject_leaves_action_rejected_and_skips_backend(gate, backend):
    """Reject path: tool call queued → operator rejects → rejected state, backend not called."""
    _, calls = backend
    waiter = _ResourceWaiter()
    async with Client(gate, message_handler=waiter) as client:
        result = await client.call_tool_mcp("test_echo", {"input": {"text": "no-run"}, "justification": "test"})
        action_id = uuid.UUID(json.loads(result.content[0].text))
        await gate.decide(action_id, DenyDecision(reason="test rejection"))
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, action_id, ActionStatus.REJECTED)
    assert calls == []


async def test_auto_approve_predicate_skips_queue(backend, tmp_path):
    """Auto-approve predicate: tool call immediately executes without any operator action."""
    srv, calls = backend
    gate = _make_gate(srv, tmp_path, lambda ns, tool, args: Approved(), db_name="gate_auto.db")
    waiter = _ResourceWaiter()
    async with Client(gate, message_handler=waiter) as client:
        result = await client.call_tool_mcp("test_echo", {"input": {"text": "auto"}, "justification": "auto"})
        action_id = uuid.UUID(json.loads(result.content[0].text))
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, action_id, ActionStatus.DONE)
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
    async with Client(gate, message_handler=waiter) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "alpha_echo" in tool_names
        assert "beta_echo" in tool_names

        # Call alpha backend
        result_a = await client.call_tool_mcp("alpha_echo", {"input": {"text": "from-a"}, "justification": "test"})
        action_id_a = uuid.UUID(json.loads(result_a.content[0].text))
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, action_id_a, ActionStatus.DONE)

        # Call beta backend
        result_b = await client.call_tool_mcp("beta_echo", {"input": {"text": "from-b"}, "justification": "test"})
        action_id_b = uuid.UUID(json.loads(result_b.content[0].text))
        with anyio.fail_after(5.0):
            await waiter.wait_for(client, action_id_b, ActionStatus.DONE)

    assert calls_a == [{"text": "from-a"}]
    assert calls_b == [{"text": "from-b"}]


if __name__ == "__main__":
    pytest_bazel.main()
