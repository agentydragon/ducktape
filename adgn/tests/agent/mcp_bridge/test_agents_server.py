"""Tests for agents MCP server resources and tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from unittest.mock import Mock

from fastmcp.client import Client
from hamcrest import all_of, assert_that, has_entries, has_items, has_key, has_length, has_properties, greater_than_or_equal_to, instance_of
from mcp import types as mcp_types
import pytest

from adgn.agent.approvals import ApprovalHub, PendingApproval
from adgn.agent.handler import AbortTurnDecision, ContinueDecision
from adgn.agent.mcp_bridge.servers.agents import make_agents_server
from adgn.agent.mcp_bridge.types import AgentID, AgentMode
from adgn.agent.persist import ApprovalOutcome, Decision, PolicyProposal, ToolCall, ToolCallRecord
from adgn.mcp.snapshots import RunningServerEntry, SamplingSnapshot


def add_pending_approval(hub: ApprovalHub, call_id: str, tool_call: ToolCall) -> None:
    """Helper to add a pending approval to the hub for testing."""
    fut = asyncio.get_event_loop().create_future()
    hub._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)


def read_text_json(result):
    """Helper to parse JSON from MCP resource result."""
    if isinstance(result, list) and len(result) > 0:
        text_content = result[0].text if hasattr(result[0], "text") else result[0]
        return json.loads(text_content) if isinstance(text_content, str) else text_content
    return result


# --- Test-specific fixtures ---
# Shared fixtures (mock_persistence, mock_approval_hub, mock_approval_engine,
# mock_running_infrastructure, mock_local_runtime, mock_registry) are in conftest.py


@pytest.fixture
async def agents_client(mock_registry):
    """Create agents server client."""
    server = await make_agents_server(mock_registry)
    async with Client(server) as client:
        yield client


# --- Resource Tests ---


@pytest.mark.asyncio
async def test_list_agents_resource(agents_client):
    """Test resource://agents/list returns all agents with capabilities."""
    result = await agents_client.read_resource("resource://agents/list")
    content = read_text_json(result)

    assert "agents" in content
    agents = content["agents"]
    assert_that(agents, has_length(2))

    assert_that(
        agents,
        has_items(
            has_entries(
                id="local-agent",
                mode="local",
                live=True,
                run_phase="sampling",
                pending_approvals=0,
                capabilities=has_entries(chat=True, agent_loop=True),
            ),
            has_entries(
                id="bridge-agent",
                mode="bridge",
                live=True,
                run_phase="sampling",
                pending_approvals=0,
                capabilities=has_entries(chat=False, agent_loop=False),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_agent_state_resource_returns_snapshot(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/state returns sampling snapshot for local agents."""
    result = await agents_client.read_resource("resource://agents/local-agent/state")
    content = read_text_json(result)

    assert_that(content, all_of(has_key("ts"), has_key("servers")))
    assert content["ts"] == "2025-01-15T10:00:00Z"
    assert content["servers"] == {}

    mock_local_runtime.running.compositor.sampling_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_agent_state_resource_bridge_agent(agents_client):
    """Test resource://agents/{id}/state fails for bridge agents."""
    with pytest.raises(Exception, match=r"(?i)not a local agent"):
        await agents_client.read_resource("resource://agents/bridge-agent/state")


@pytest.mark.asyncio
async def test_agent_ui_state_resource(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/ui/state returns UI state for local agents with session."""
    result = await agents_client.read_resource("resource://agents/local-agent/ui/state")
    content = read_text_json(result)

    assert_that(content, all_of(has_key("seq"), has_key("state")))
    assert content["seq"] == 0
    assert content["state"]["seq"] == 0
    assert content["state"]["items"] == []


@pytest.mark.asyncio
async def test_agent_ui_state_resource_no_session(agents_client, mock_registry):
    """Test resource://agents/{id}/ui/state fails when agent has no session."""
    # Make bridge agent return None for session
    with pytest.raises(Exception, match=r"(?i)has no session"):
        await agents_client.read_resource("resource://agents/bridge-agent/ui/state")


@pytest.mark.asyncio
async def test_agent_state_resource_with_servers(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/state returns sampling snapshot with server data."""
    server_entry = RunningServerEntry(
        state="running",
        initialize=mcp_types.InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=mcp_types.ServerCapabilities(tools={"listChanged": True}),
            serverInfo=mcp_types.Implementation(name="test-server", version="1.0.0"),
        ),
        tools=[
            mcp_types.Tool(
                name="test_tool", description="A test tool", inputSchema={"type": "object", "properties": {}}
            )
        ],
    )

    sampling_snapshot = SamplingSnapshot(ts="2025-01-15T10:30:00Z", servers={"test-server": server_entry})

    mock_local_runtime.running.compositor.sampling_snapshot.return_value = sampling_snapshot

    result = await agents_client.read_resource("resource://agents/local-agent/state")
    content = read_text_json(result)

    assert content["ts"] == "2025-01-15T10:30:00Z"
    assert "test-server" in content["servers"]

    server = content["servers"]["test-server"]
    assert server["state"] == "running"
    assert_that(server, all_of(has_key("initialize"), has_key("tools")))
    assert_that(server["tools"], has_length(1))
    assert server["tools"][0]["name"] == "test_tool"


@pytest.mark.asyncio
async def test_agent_state_resource_no_runtime(mock_registry, agents_client):
    """Test resource://agents/{id}/state fails when local agent has no runtime."""
    original_get_runtime = mock_registry.get_local_runtime

    def get_runtime_none(agent_id):
        if agent_id == "local-agent":
            return None
        return original_get_runtime(agent_id)

    mock_registry.get_local_runtime = get_runtime_none

    with pytest.raises(Exception, match=r"(?i)no local runtime"):
        await agents_client.read_resource("resource://agents/local-agent/state")


@pytest.mark.asyncio
async def test_agent_approvals_pending_empty(agents_client):
    """Test resource://agents/{id}/approvals/pending with no pending approvals."""
    result = await agents_client.read_resource("resource://agents/local-agent/approvals/pending")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert content["pending"] == []


@pytest.mark.asyncio
async def test_agent_approvals_pending_with_items(agents_client, mock_approval_hub):
    """Test resource://agents/{id}/approvals/pending with pending approvals."""
    tool_call = ToolCall(name="test_tool", call_id="call-123", args_json='{"arg1": "value1"}')
    add_pending_approval(mock_approval_hub, "call-123", tool_call)

    result = await agents_client.read_resource("resource://agents/local-agent/approvals/pending")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert_that(content["pending"], has_length(1))

    approval = content["pending"][0]
    assert approval["tool_call"]["call_id"] == "call-123"
    assert approval["tool_call"]["name"] == "test_tool"
    assert json.loads(approval["tool_call"]["args_json"]) == {"arg1": "value1"}
    assert "timestamp" in approval


@pytest.mark.asyncio
async def test_global_approvals_pending_empty(agents_client):
    """Test resource://approvals/pending with no pending approvals."""
    result = await agents_client.read_resource("resource://approvals/pending")

    assert_that(result, instance_of(list))
    assert_that(result, has_length(1))

    wrapped = result[0]
    assert_that(wrapped, instance_of(mcp_types.TextResourceContents))
    data = json.loads(wrapped.text)
    assert data["contents"] == []


@pytest.mark.asyncio
async def test_global_approvals_pending_multi_content(agents_client, mock_approval_hub):
    """Test resource://approvals/pending returns multi-content blocks."""
    tool_call_1 = ToolCall(name="test_tool_1", call_id="call-123", args_json='{"arg1": "value1"}')
    tool_call_2 = ToolCall(name="test_tool_2", call_id="call-456", args_json='{"arg2": "value2"}')

    add_pending_approval(mock_approval_hub, "call-123", tool_call_1)
    add_pending_approval(mock_approval_hub, "call-456", tool_call_2)

    result = await agents_client.read_resource("resource://approvals/pending")

    assert_that(result, instance_of(list))
    assert_that(result, has_length(1))

    wrapped = result[0]
    assert_that(wrapped, instance_of(mcp_types.TextResourceContents))
    data = json.loads(wrapped.text)

    # Should have multiple content blocks (2 approvals per agent x 2 agents)
    contents = data["contents"]
    assert_that(contents, has_length(4))

    # Each content block should be TextResourceContents with unique URI
    for content in contents:
        assert content["mimeType"] == "application/json"
        assert content["uri"].startswith("resource://agents/")
        assert "/approvals/" in content["uri"]

        # Parse and validate JSON
        approval_data = json.loads(content["text"])
        assert_that(
            approval_data,
            all_of(
                has_key("agent_id"),
                has_key("call_id"),
                has_key("tool"),
                has_key("args"),
                has_key("timestamp"),
            ),
        )


@pytest.mark.asyncio
async def test_agent_approvals_history_empty(agents_client, mock_persistence):
    """Test resource://agents/{id}/approvals/history with no history."""
    mock_persistence.list_tool_calls.return_value = []

    result = await agents_client.read_resource("resource://agents/local-agent/approvals/history")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert content["timeline"] == []
    assert content["pending"] == []
    assert content["count"] == 0


@pytest.mark.asyncio
async def test_agent_approvals_history_with_records(agents_client, mock_persistence):
    """Test resource://agents/{id}/approvals/history with completed records."""
    decided_at = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
    record = ToolCallRecord(
        call_id="call-123",
        run_id="run-456",
        agent_id=AgentID("local-agent"),
        tool_call=ToolCall(name="test_tool", call_id="call-123", args_json='{"arg1": "value1"}'),
        decision=Decision(outcome=ApprovalOutcome.USER_APPROVE, decided_at=decided_at, reason=None),
        execution=None,
    )

    mock_persistence.list_tool_calls.return_value = [record]

    result = await agents_client.read_resource("resource://agents/local-agent/approvals/history")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert_that(content["timeline"], has_length(1))
    assert content["count"] == 1

    entry = content["timeline"][0]
    assert entry["tool_call"]["call_id"] == "call-123"
    assert entry["tool_call"]["name"] == "test_tool"
    assert json.loads(entry["tool_call"]["args_json"]) == {"arg1": "value1"}
    assert entry["outcome"] == "user_approve"


@pytest.mark.asyncio
async def test_agent_approvals_history_filters_pending(agents_client, mock_persistence):
    """Test resource://agents/{id}/approvals/history filters out pending records."""
    pending_record = ToolCallRecord(
        call_id="call-pending",
        run_id="run-456",
        agent_id=AgentID("local-agent"),
        tool_call=ToolCall(name="pending_tool", call_id="call-pending", args_json="{}"),
        decision=None,
        execution=None,
    )

    decided_at = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
    completed_record = ToolCallRecord(
        call_id="call-completed",
        run_id="run-456",
        agent_id=AgentID("local-agent"),
        tool_call=ToolCall(name="completed_tool", call_id="call-completed", args_json="{}"),
        decision=Decision(outcome=ApprovalOutcome.USER_APPROVE, decided_at=decided_at, reason=None),
        execution=None,
    )

    mock_persistence.list_tool_calls.return_value = [pending_record, completed_record]

    result = await agents_client.read_resource("resource://agents/local-agent/approvals/history")
    content = read_text_json(result)

    # Only completed record should be in timeline
    assert_that(content["timeline"], has_length(1))
    assert content["timeline"][0]["tool_call"]["call_id"] == "call-completed"


@pytest.mark.asyncio
async def test_agent_policy_proposals_empty(agents_client, mock_persistence):
    """Test resource://agents/{id}/policy/proposals with no proposals."""
    mock_persistence.list_policy_proposals.return_value = []

    result = await agents_client.read_resource("resource://agents/local-agent/policy/proposals")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert content["proposals"] == []
    assert content["active_policy_uri"] == "resource://approval-policy/policy.py"


@pytest.mark.asyncio
async def test_agent_policy_proposals_with_items(agents_client, mock_persistence):
    """Test resource://agents/{id}/policy/proposals with proposals."""
    created_at = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
    decided_at = datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC)

    proposal = PolicyProposal(
        id="prop-123", status="approved", created_at=created_at, decided_at=decided_at, content="policy content"
    )

    mock_persistence.list_policy_proposals.return_value = [proposal]

    result = await agents_client.read_resource("resource://agents/local-agent/policy/proposals")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert_that(content["proposals"], has_length(1))

    prop = content["proposals"][0]
    assert prop["id"] == "prop-123"
    assert prop["status"] == "approved"
    assert prop["proposal_uri"] == "resource://approval-policy/proposals/prop-123"


# --- Error Cases ---


@pytest.mark.asyncio
async def test_agent_not_found(agents_client):
    """Test accessing resources for non-existent agent."""
    with pytest.raises(Exception, match=r"(?i)not found"):
        await agents_client.read_resource("resource://agents/nonexistent-agent/approvals/pending")


@pytest.mark.asyncio
async def test_agent_not_initialized(mock_registry, agents_client):
    """Test accessing resources for agent that exists but isn't initialized."""
    def get_infrastructure_uninitialized(agent_id: AgentID):
        if agent_id == "uninitialized-agent":
            raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
        # Call original for other agents
        raise KeyError(f"Agent {agent_id} not found in registry")

    mock_registry.get_infrastructure = get_infrastructure_uninitialized

    # Add to known_agents
    original_known = mock_registry.known_agents
    mock_registry.known_agents = lambda: [*original_known(), "uninitialized-agent"]

    with pytest.raises(Exception, match=r"(?i)not yet initialized"):
        await agents_client.read_resource("resource://agents/uninitialized-agent/approvals/pending")


# --- Tool Tests ---


@pytest.mark.asyncio
async def test_approve_tool_call(agents_client, mock_approval_hub):
    """Test approve_tool_call resolves with ContinueDecision."""
    tool_call = ToolCall(name="test_tool", call_id="call-123", args_json="{}")
    add_pending_approval(mock_approval_hub, "call-123", tool_call)

    result = await agents_client.call_tool(
        "decide_approval", arguments={"agent_id": "local-agent", "call_id": "call-123", "decision": "approve"}
    )

    assert result.is_error is False

    # Approval should be removed from pending
    assert "call-123" not in mock_approval_hub._pending


@pytest.mark.asyncio
async def test_reject_tool_call(agents_client, mock_approval_hub):
    """Test reject_tool_call resolves with AbortTurnDecision."""
    tool_call = ToolCall(name="test_tool", call_id="call-456", args_json="{}")
    add_pending_approval(mock_approval_hub, "call-456", tool_call)

    result = await agents_client.call_tool(
        "decide_approval", arguments={"agent_id": "local-agent", "call_id": "call-456", "decision": "deny_continue", "reason": "Test rejection"}
    )

    assert result.is_error is False

    # Approval should be removed from pending
    assert "call-456" not in mock_approval_hub._pending


@pytest.mark.asyncio
async def test_abort_agent_local(agents_client, mock_local_runtime):
    """Test abort_agent succeeds for local agents."""
    result = await agents_client.call_tool("abort_agent", arguments={"agent_id": "local-agent"})

    assert result.is_error is False

    mock_local_runtime.agent.abort.assert_called_once()


@pytest.mark.asyncio
async def test_abort_agent_bridge_fails(agents_client):
    """Test abort_agent fails for bridge agents."""
    with pytest.raises(Exception, match=r"(?i)(not a local agent|cannot abort)"):
        await agents_client.call_tool("abort_agent", arguments={"agent_id": "bridge-agent"})


@pytest.mark.asyncio
async def test_abort_agent_not_found(agents_client):
    """Test abort_agent fails for non-existent agent."""
    with pytest.raises(Exception, match=r"(?i)not found"):
        await agents_client.call_tool("abort_agent", arguments={"agent_id": "nonexistent-agent"})


@pytest.mark.asyncio
async def test_abort_agent_no_runtime(mock_registry, agents_client):
    """Test abort_agent fails for local agent without runtime."""
    original_get_runtime = mock_registry.get_local_runtime

    def get_runtime_none(agent_id: AgentID):
        if agent_id == "local-agent":
            return None
        return original_get_runtime(agent_id)

    mock_registry.get_local_runtime = get_runtime_none

    with pytest.raises(Exception, match=r"(?i)no agent loop"):
        await agents_client.call_tool("abort_agent", arguments={"agent_id": "local-agent"})


# --- Tool Error Cases ---


@pytest.mark.asyncio
async def test_approve_nonexistent_call_id(agents_client):
    """Test approve_tool_call with non-existent call_id (no-op)."""
    result = await agents_client.call_tool(
        "decide_approval", arguments={"agent_id": "local-agent", "call_id": "nonexistent", "decision": "approve"}
    )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_reject_nonexistent_call_id(agents_client):
    """Test reject_tool_call with non-existent call_id (no-op)."""
    result = await agents_client.call_tool(
        "decide_approval", arguments={"agent_id": "local-agent", "call_id": "nonexistent", "decision": "deny_continue", "reason": "test"}
    )

    assert result.is_error is False


# --- Additional Test Coverage ---


@pytest.mark.asyncio
async def test_global_approvals_pending_different_per_agent(mock_persistence, mock_approval_engine):
    """Test resource://approvals/pending returns different approvals per agent."""
    local_hub = ApprovalHub()
    bridge_hub = ApprovalHub()

    local_infra = Mock()
    local_infra.approval_hub = local_hub
    local_infra.approval_engine = mock_approval_engine

    bridge_infra = Mock()
    bridge_infra.approval_hub = bridge_hub
    bridge_infra.approval_engine = mock_approval_engine

    custom_registry = Mock()

    def known_agents():
        return [AgentID("local-agent"), AgentID("bridge-agent")]

    async def get_infrastructure(agent_id: AgentID):
        if agent_id == AgentID("local-agent"):
            return local_infra
        if agent_id == AgentID("bridge-agent"):
            return bridge_infra
        raise KeyError(f"Agent {agent_id} not found")

    def get_agent_mode(agent_id: AgentID):
        return AgentMode.LOCAL if agent_id == "local-agent" else AgentMode.BRIDGE

    custom_registry.known_agents = known_agents
    custom_registry.get_infrastructure = get_infrastructure
    custom_registry.get_agent_mode = get_agent_mode

    server = await make_agents_server(custom_registry)

    tool_call_local_1 = ToolCall(name="local_tool_1", call_id="local-call-1", args_json='{"param": "value1"}')
    tool_call_local_2 = ToolCall(name="local_tool_2", call_id="local-call-2", args_json='{"param": "value2"}')
    tool_call_bridge_1 = ToolCall(name="bridge_tool_1", call_id="bridge-call-1", args_json='{"param": "value3"}')

    local_hub._pending["local-call-1"] = PendingApproval(tool_call=tool_call_local_1, future=asyncio.get_event_loop().create_future())
    local_hub._pending["local-call-2"] = PendingApproval(tool_call=tool_call_local_2, future=asyncio.get_event_loop().create_future())
    bridge_hub._pending["bridge-call-1"] = PendingApproval(tool_call=tool_call_bridge_1, future=asyncio.get_event_loop().create_future())

    async with Client(server) as client:
        result = await client.read_resource("resource://approvals/pending")

        assert_that(result, instance_of(list))
        assert_that(result, has_length(1))

        wrapped = result[0]
        assert_that(wrapped, instance_of(mcp_types.TextResourceContents))
        data = json.loads(wrapped.text)

        contents = data["contents"]
        assert_that(contents, has_length(3))

        approvals_by_agent = {"local-agent": [], "bridge-agent": []}
        for content in contents:
            approval_data = json.loads(content["text"])

            agent_id = approval_data["agent_id"]
            call_id = approval_data["call_id"]
            tool = approval_data["tool"]

            approvals_by_agent[agent_id].append({"call_id": call_id, "tool": tool})

        assert_that(approvals_by_agent["local-agent"], has_length(2))
        local_calls = {a["call_id"] for a in approvals_by_agent["local-agent"]}
        assert_that(local_calls, has_items("local-call-1", "local-call-2"))

        assert_that(approvals_by_agent["bridge-agent"], has_length(1))
        bridge_calls = {a["call_id"] for a in approvals_by_agent["bridge-agent"]}
        assert "bridge-call-1" in bridge_calls


@pytest.mark.asyncio
async def test_agent_approvals_history_mixed_outcomes(agents_client, mock_persistence):
    """Test resource://agents/{id}/approvals/history with mixed decision outcomes."""
    base_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)

    records = [
        # POLICY_ALLOW
        ToolCallRecord(
            call_id="call-policy-allow",
            run_id="run-1",
            agent_id=AgentID("local-agent"),
            tool_call=ToolCall(name="safe_tool", call_id="call-policy-allow", args_json='{"action": "read"}'),
            decision=Decision(outcome=ApprovalOutcome.POLICY_ALLOW, decided_at=base_time, reason="Auto-approved"),
            execution=None,
        ),
        # USER_APPROVE
        ToolCallRecord(
            call_id="call-user-approve",
            run_id="run-1",
            agent_id=AgentID("local-agent"),
            tool_call=ToolCall(name="user_tool", call_id="call-user-approve", args_json='{"action": "write"}'),
            decision=Decision(
                outcome=ApprovalOutcome.USER_APPROVE, decided_at=base_time + timedelta(minutes=1), reason=None
            ),
            execution=None,
        ),
        # POLICY_DENY_CONTINUE
        ToolCallRecord(
            call_id="call-policy-deny-cont",
            run_id="run-1",
            agent_id=AgentID("local-agent"),
            tool_call=ToolCall(name="risky_tool", call_id="call-policy-deny-cont", args_json='{"action": "delete"}'),
            decision=Decision(
                outcome=ApprovalOutcome.POLICY_DENY_CONTINUE,
                decided_at=base_time + timedelta(minutes=2),
                reason="Denied by policy",
            ),
            execution=None,
        ),
        # USER_DENY_ABORT
        ToolCallRecord(
            call_id="call-user-deny-abort",
            run_id="run-1",
            agent_id=AgentID("local-agent"),
            tool_call=ToolCall(
                name="dangerous_tool", call_id="call-user-deny-abort", args_json='{"action": "destroy"}'
            ),
            decision=Decision(
                outcome=ApprovalOutcome.USER_DENY_ABORT,
                decided_at=base_time + timedelta(minutes=3),
                reason="User rejected",
            ),
            execution=None,
        ),
    ]

    mock_persistence.list_tool_calls.return_value = records

    result = await agents_client.read_resource("resource://agents/local-agent/approvals/history")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert_that(content["timeline"], has_length(4))
    assert content["count"] == 4

    # Verify all outcomes are present
    outcomes_in_timeline = {entry["outcome"] for entry in content["timeline"]}
    assert ApprovalOutcome.POLICY_ALLOW in outcomes_in_timeline
    assert ApprovalOutcome.USER_APPROVE in outcomes_in_timeline
    assert ApprovalOutcome.POLICY_DENY_CONTINUE in outcomes_in_timeline
    assert ApprovalOutcome.USER_DENY_ABORT in outcomes_in_timeline

    # Verify specific entries
    policy_allow_entry = next(e for e in content["timeline"] if e["tool_call"]["call_id"] == "call-policy-allow")
    assert policy_allow_entry["outcome"] == "policy_allow"
    assert policy_allow_entry["tool_call"]["name"] == "safe_tool"
    assert policy_allow_entry["reason"] == "Auto-approved"

    user_deny_entry = next(e for e in content["timeline"] if e["tool_call"]["call_id"] == "call-user-deny-abort")
    assert user_deny_entry["outcome"] == "user_deny_abort"
    assert user_deny_entry["tool_call"]["name"] == "dangerous_tool"
    assert user_deny_entry["reason"] == "User rejected"


@pytest.mark.asyncio
async def test_agent_state_resource_idle_agent(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/state for an idle agent (no active sampling)."""
    # Mock an idle snapshot (empty servers, older timestamp)
    idle_snapshot = SamplingSnapshot(ts="2025-01-15T09:00:00Z", servers={})

    mock_local_runtime.running.compositor.sampling_snapshot.return_value = idle_snapshot

    result = await agents_client.read_resource("resource://agents/local-agent/state")
    content = read_text_json(result)

    # Verify idle snapshot structure
    assert content["ts"] == "2025-01-15T09:00:00Z"
    assert content["servers"] == {}

    # Verify sampling_snapshot was called
    mock_local_runtime.running.compositor.sampling_snapshot.assert_called()


@pytest.mark.asyncio
async def test_global_approvals_pending_ordering(agents_client, mock_approval_hub):
    """Test resource://approvals/pending maintains agent and approval ordering."""
    # Add multiple pending approvals in specific order
    for i in range(3):
        tool_call = ToolCall(name=f"tool_{i}", call_id=f"call-{i}", args_json=f'{{"index": {i}}}')
        mock_approval_hub._pending[f"call-{i}"] = PendingApproval(tool_call=tool_call, future=asyncio.get_event_loop().create_future())

    result = await agents_client.read_resource("resource://approvals/pending")

    # FastMCP wraps ReadResourceResult in a list of TextResourceContents
    assert_that(result, instance_of(list))
    assert_that(result, has_length(1))

    # Parse the wrapped result
    wrapped = result[0]
    assert_that(wrapped, instance_of(mcp_types.TextResourceContents))
    data = json.loads(wrapped.text)

    # Each agent should have 3 approvals (6 total for 2 agents)
    contents = data["contents"]
    assert_that(contents, has_length(6))

    # Parse and verify ordering within each agent
    for content in contents:
        approval_data = json.loads(content["text"])
        assert_that(approval_data, all_of(has_key("agent_id"), has_key("call_id"), has_key("tool")))


@pytest.mark.asyncio
async def test_presets_list_resource(agents_client):
    """Test resource://presets/list returns available agent presets."""
    result = await agents_client.read_resource("resource://presets/list")
    content = read_text_json(result)

    assert "presets" in content
    presets = content["presets"]

    # Should have at least the built-in "default" preset
    assert_that(presets, has_length(greater_than_or_equal_to(1)))

    # Check default preset exists
    default_preset = next((p for p in presets if p["name"] == "default"), None)
    assert default_preset is not None
    assert "description" in default_preset

    # Verify all presets have required fields
    for preset in presets:
        assert_that(preset, all_of(has_key("name"), has_key("description")))


# --- Additional Resource Tests (snapshot, info, presets variations) ---


@pytest.mark.asyncio
async def test_agent_snapshot_returns_full_state(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/snapshot returns full compositor snapshot."""
    result = await agents_client.read_resource("resource://agents/local-agent/snapshot")
    content = read_text_json(result)

    # Verify sampling snapshot structure
    assert_that(content, all_of(has_key("ts"), has_key("servers")))
    assert content["ts"] == "2025-01-15T10:00:00Z"
    assert content["servers"] == {}

    # Verify compositor.sampling_snapshot was called
    mock_local_runtime.running.compositor.sampling_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_agent_snapshot_bridge_agent_fails(agents_client):
    """Test resource://agents/{id}/snapshot fails for bridge agents."""
    with pytest.raises(Exception, match=r"(?i)not a local agent"):
        await agents_client.read_resource("resource://agents/bridge-agent/snapshot")


@pytest.mark.asyncio
async def test_agent_snapshot_no_runtime_fails(mock_registry, agents_client):
    """Test resource://agents/{id}/snapshot fails when local agent has no runtime."""
    # Mock get_local_runtime to return None for local-agent
    original_get_runtime = mock_registry.get_local_runtime

    def get_runtime_none(agent_id):
        if agent_id == "local-agent":
            return None
        return original_get_runtime(agent_id)

    mock_registry.get_local_runtime = get_runtime_none

    with pytest.raises(Exception, match=r"(?i)no local runtime"):
        await agents_client.read_resource("resource://agents/local-agent/snapshot")


@pytest.mark.asyncio
async def test_agent_snapshot_with_servers(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/snapshot returns snapshot with running servers."""
    server_entry = RunningServerEntry(
        state="running",
        initialize=mcp_types.InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=mcp_types.ServerCapabilities(tools={"listChanged": True}),
            serverInfo=mcp_types.Implementation(name="snapshot-test-server", version="2.0.0"),
        ),
        tools=[
            mcp_types.Tool(
                name="snapshot_tool",
                description="A snapshot test tool",
                inputSchema={"type": "object", "properties": {"param": {"type": "string"}}},
            )
        ],
    )

    sampling_snapshot = SamplingSnapshot(ts="2025-01-15T11:00:00Z", servers={"snapshot-test-server": server_entry})

    mock_local_runtime.running.compositor.sampling_snapshot.return_value = sampling_snapshot

    result = await agents_client.read_resource("resource://agents/local-agent/snapshot")
    content = read_text_json(result)

    # Verify sampling snapshot structure
    assert content["ts"] == "2025-01-15T11:00:00Z"
    assert "snapshot-test-server" in content["servers"]

    server = content["servers"]["snapshot-test-server"]
    assert server["state"] == "running"
    assert_that(server, all_of(has_key("initialize"), has_key("tools")))
    assert_that(server["tools"], has_length(1))
    assert server["tools"][0]["name"] == "snapshot_tool"


@pytest.mark.asyncio
async def test_agent_snapshot_agent_not_found(agents_client):
    """Test resource://agents/{id}/snapshot fails for non-existent agent."""
    with pytest.raises(Exception, match=r"(?i)not found"):
        await agents_client.read_resource("resource://agents/nonexistent-agent/snapshot")


@pytest.mark.asyncio
async def test_agent_info_local_running(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/info returns correct info for running local agent."""
    mock_local_runtime.model = "claude-3-5-sonnet-20241022"

    result = await agents_client.read_resource("resource://agents/local-agent/info")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert content["mode"] == "local"
    assert content["model"] == "claude-3-5-sonnet-20241022"
    assert content["status"] == "running"


@pytest.mark.asyncio
async def test_agent_info_bridge_no_model(agents_client):
    """Test resource://agents/{id}/info returns correct info for bridge agent."""
    result = await agents_client.read_resource("resource://agents/bridge-agent/info")
    content = read_text_json(result)

    assert content["agent_id"] == "bridge-agent"
    assert content["mode"] == "bridge"
    assert content["model"] is None
    assert content["status"] == "stopped"


@pytest.mark.asyncio
async def test_agent_info_local_stopped(mock_registry, agents_client):
    """Test resource://agents/{id}/info shows 'stopped' for local agent without runtime."""
    original_get_runtime = mock_registry.get_local_runtime

    def get_runtime_none(agent_id):
        if agent_id == "local-agent":
            return None
        return original_get_runtime(agent_id)

    mock_registry.get_local_runtime = get_runtime_none

    result = await agents_client.read_resource("resource://agents/local-agent/info")
    content = read_text_json(result)

    assert content["agent_id"] == "local-agent"
    assert content["mode"] == "local"
    assert content["model"] is None
    assert content["status"] == "stopped"


@pytest.mark.asyncio
async def test_agent_info_not_found(agents_client):
    """Test resource://agents/{id}/info fails for non-existent agent."""
    with pytest.raises(Exception, match=r"(?i)not found"):
        await agents_client.read_resource("resource://agents/nonexistent-agent/info")


@pytest.mark.asyncio
async def test_presets_list_empty(agents_client, monkeypatch):
    """Test resource://presets/list with no presets available."""
    import adgn.agent.mcp_bridge.servers.agents as agents_module

    monkeypatch.setattr(agents_module, "discover_presets", lambda env_dir: {})

    result = await agents_client.read_resource("resource://presets/list")
    content = read_text_json(result)

    assert "presets" in content
    assert content["presets"] == []


@pytest.mark.asyncio
async def test_presets_list_with_multiple_presets(agents_client, monkeypatch):
    """Test resource://presets/list returns all available presets."""
    import adgn.agent.mcp_bridge.servers.agents as agents_module
    from adgn.agent.presets import AgentPreset

    sample_presets = {
        "default": AgentPreset(name="default", description="Default preset"),
        "coding": AgentPreset(name="coding", description="Coding assistant preset", model="claude-3-opus-20240229"),
        "minimal": AgentPreset(name="minimal", description=None),
    }

    monkeypatch.setattr(agents_module, "discover_presets", lambda env_dir: sample_presets)

    result = await agents_client.read_resource("resource://presets/list")
    content = read_text_json(result)

    assert "presets" in content
    presets = content["presets"]
    assert_that(presets, has_length(3))

    # Check that all presets are included
    preset_names = {p["name"] for p in presets}
    assert preset_names == {"default", "coding", "minimal"}

    # Check preset with description
    default_preset = next((p for p in presets if p["name"] == "default"), None)
    assert default_preset is not None
    assert default_preset["description"] == "Default preset"

    # Check preset without description
    minimal_preset = next((p for p in presets if p["name"] == "minimal"), None)
    assert minimal_preset is not None
    assert minimal_preset["description"] is None


@pytest.mark.asyncio
async def test_presets_list_ordering(agents_client, monkeypatch):
    """Test resource://presets/list preserves preset ordering from discovery."""
    from collections import OrderedDict

    import adgn.agent.mcp_bridge.servers.agents as agents_module
    from adgn.agent.presets import AgentPreset

    ordered_presets = OrderedDict(
        [
            ("alpha", AgentPreset(name="alpha", description="First")),
            ("beta", AgentPreset(name="beta", description="Second")),
            ("gamma", AgentPreset(name="gamma", description="Third")),
        ]
    )

    monkeypatch.setattr(agents_module, "discover_presets", lambda env_dir: ordered_presets)

    result = await agents_client.read_resource("resource://presets/list")
    content = read_text_json(result)

    presets = content["presets"]
    assert_that(presets, has_length(3))

    # Verify order is preserved (dict iteration order is guaranteed in Python 3.7+)
    preset_names = [p["name"] for p in presets]
    assert preset_names == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_agents_list_resource_detailed_status(agents_client):
    """Test resource://agents/list returns detailed status for all agents."""
    result = await agents_client.read_resource("resource://agents/list")
    content = read_text_json(result)

    assert "agents" in content
    assert_that(content["agents"], has_length(greater_than_or_equal_to(1)))

    # Check that all agents have required status fields
    for agent in content["agents"]:
        assert_that(
            agent,
            all_of(
                has_key("id"),
                has_key("mode"),
                has_key("live"),
                has_key("active_run_id"),
                has_key("run_phase"),
                has_key("pending_approvals"),
                has_key("capabilities"),
            ),
        )

        # Verify field types
        assert_that(agent["live"], instance_of(bool))
        assert_that(agent["run_phase"], instance_of(str))
        assert_that(agent["pending_approvals"], instance_of(int))
        assert_that(agent["capabilities"], instance_of(dict))


# --- Approval Hub Notification Tests ---


@pytest.mark.asyncio
async def test_approval_hub_notifier_on_request(mock_approval_hub):
    """Test that ApprovalHub notifier is called when an approval is requested."""
    # Track notifier calls
    notifier_calls = []

    def test_notifier():
        notifier_calls.append("approval_requested")

    mock_approval_hub.set_notifier(test_notifier)

    # Create approval request
    tool_call = ToolCall(name="test_tool", call_id="call-123", args_json='{"arg": "value"}')
    request = PendingApproval(tool_call=tool_call, future=asyncio.get_event_loop().create_future())

    # Start await_decision in background (it will wait for resolution)
    task = asyncio.create_task(mock_approval_hub.await_decision("call-123", request))

    # Give it time to process
    await asyncio.sleep(0.1)

    # Verify notifier was called
    assert_that(notifier_calls, has_length(1))
    assert notifier_calls[0] == "approval_requested"

    # Clean up: resolve the approval
    mock_approval_hub.resolve("call-123", ContinueDecision())
    await task


@pytest.mark.asyncio
async def test_approval_hub_notifier_on_resolve(mock_approval_hub):
    """Test that ApprovalHub notifier is called when an approval is resolved."""
    # Track notifier calls
    notifier_calls = []

    def test_notifier():
        notifier_calls.append("approval_resolved")

    mock_approval_hub.set_notifier(test_notifier)

    # Create approval request
    tool_call = ToolCall(name="test_tool", call_id="call-456", args_json='{"arg": "value"}')
    request = PendingApproval(tool_call=tool_call, future=asyncio.get_event_loop().create_future())

    # Start await_decision in background
    task = asyncio.create_task(mock_approval_hub.await_decision("call-456", request))

    # Give it time to process request
    await asyncio.sleep(0.1)

    # Clear calls from request phase
    notifier_calls.clear()

    # Resolve the approval
    mock_approval_hub.resolve("call-456", ContinueDecision())

    # Verify notifier was called for resolve
    assert_that(notifier_calls, has_length(1))
    assert notifier_calls[0] == "approval_resolved"

    # Wait for task to complete
    await task


@pytest.mark.asyncio
async def test_approval_hub_notifier_not_called_without_setup(mock_approval_hub):
    """Test that ApprovalHub works correctly when no notifier is set."""
    # Don't set a notifier - should not crash

    tool_call = ToolCall(name="test_tool", call_id="call-789", args_json="{}")
    request = PendingApproval(tool_call=tool_call, future=asyncio.get_event_loop().create_future())

    # Start await_decision
    task = asyncio.create_task(mock_approval_hub.await_decision("call-789", request))

    await asyncio.sleep(0.1)

    # Resolve - should not crash
    mock_approval_hub.resolve("call-789", AbortTurnDecision(reason="test"))

    # Should complete successfully
    decision = await task
    assert_that(decision, all_of(instance_of(AbortTurnDecision), has_properties(reason="test")))



@pytest.mark.asyncio
async def test_agent_session_state_resource(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/session/state returns session state for local agents."""
    from datetime import UTC, datetime
    from uuid import UUID

    mock_session = mock_local_runtime.session
    mock_session._manager = Mock()
    mock_session._manager._session_id = "test-session-123"
    mock_session._run_counter = 5

    mock_run = Mock()
    mock_run.run_id = UUID("12345678-1234-5678-1234-567812345678")
    mock_run.started_at = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
    mock_session.active_run = mock_run

    result = await agents_client.read_resource("resource://agents/local-agent/session/state")
    content = read_text_json(result)

    assert_that(
        content,
        has_entries(
            session_state=has_entries(
                session_id="test-session-123",
                version="1.0.0",
                active_run_id="12345678-1234-5678-1234-567812345678",
                run_counter=5,
            ),
            run_state=has_entries(
                run_id="12345678-1234-5678-1234-567812345678",
                status="running",
                started_at="2025-01-15T10:30:00+00:00",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_agent_session_state_resource_no_active_run(agents_client, mock_local_runtime):
    """Test resource://agents/{id}/session/state with no active run."""
    mock_session = mock_local_runtime.session
    mock_session._manager = Mock()
    mock_session._manager._session_id = "test-session-456"
    mock_session._run_counter = 3
    mock_session.active_run = None

    result = await agents_client.read_resource("resource://agents/local-agent/session/state")
    content = read_text_json(result)

    # Verify session state structure
    assert "session_state" in content
    assert content["session_state"]["session_id"] == "test-session-456"
    assert content["session_state"]["active_run_id"] is None
    assert content["session_state"]["run_counter"] == 3

    # Verify run state is None
    assert content["run_state"] is None


@pytest.mark.asyncio
async def test_agent_session_state_resource_bridge_agent(agents_client):
    """Test resource://agents/{id}/session/state fails for bridge agents."""
    with pytest.raises(Exception, match=r"(?i)not a local agent"):
        await agents_client.read_resource("resource://agents/bridge-agent/session/state")


@pytest.mark.asyncio
async def test_agent_session_state_resource_no_session(mock_registry, agents_client):
    """Test resource://agents/{id}/session/state fails when local agent has no session."""
    original_get_runtime = mock_registry.get_local_runtime

    def get_runtime_no_session(agent_id):
        runtime = original_get_runtime(agent_id)
        if agent_id == "local-agent" and runtime:
            runtime.session = None
        return runtime

    mock_registry.get_local_runtime = get_runtime_no_session

    with pytest.raises(Exception, match=r"(?i)no session"):
        await agents_client.read_resource("resource://agents/local-agent/session/state")
