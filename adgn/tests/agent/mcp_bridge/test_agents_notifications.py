"""Tests for agents MCP server resource notifications.

Verifies that resource notifications are properly wired and broadcast when:
- Policy changes (via approval_engine.set_notifier)
- Approvals are approved/rejected (via approve_tool_call/reject_tool_call tools)
- Policy proposals are created/approved/rejected (via approval_engine methods)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

from fastmcp.client import Client
import pytest

from adgn.agent.approvals import ApprovalHub, PendingApproval
from adgn.agent.mcp_bridge.servers.agents import make_agents_server
from adgn.agent.mcp_bridge.types import AgentID
from adgn.agent.persist import ToolCall
from adgn.mcp._shared.constants import APPROVAL_POLICY_RESOURCE_URI


def add_pending_approval(hub: ApprovalHub, call_id: str, tool_call: ToolCall) -> None:
    """Helper to add a pending approval to the hub for testing."""
    fut = asyncio.get_event_loop().create_future()
    hub._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)

# --- Test-specific fixtures ---
# Shared fixtures (mock_persistence, mock_approval_hub, mock_approval_engine,
# mock_running_infrastructure, mock_registry_single_agent) are in conftest.py


@pytest.fixture
async def agents_client_and_server(mock_registry_single_agent):
    """Create agents server and client for testing notifications."""
    server = await make_agents_server(mock_registry_single_agent)
    async with Client(server) as client:
        yield client, server


# --- Notification Wiring Tests ---


@pytest.mark.asyncio
async def test_policy_notifier_wired_on_server_creation(mock_registry_single_agent, mock_approval_engine):
    """Test that policy engine notifier is wired when server is created."""
    _server = await make_agents_server(mock_registry_single_agent)

    # Verify that set_notifier was called on the approval engine
    assert mock_approval_engine._notifier is not None


@pytest.mark.asyncio
async def test_policy_change_broadcasts_notification(agents_client_and_server, mock_approval_engine):
    """Test that policy changes trigger resource update notifications."""
    client, server = agents_client_and_server

    # Track notifications
    notifications_received = []

    # Mock the server's broadcast method to capture notifications
    original_broadcast = server.broadcast_resource_updated

    async def track_broadcast(uri: str):
        notifications_received.append(uri)
        await original_broadcast(uri)

    server.broadcast_resource_updated = track_broadcast

    # Simulate policy change by calling the notifier
    notifier = mock_approval_engine._notifier
    assert notifier is not None

    # Call the notifier (simulating policy engine calling it)
    notifier(APPROVAL_POLICY_RESOURCE_URI)

    # Give time for notification to be scheduled
    await asyncio.sleep(0.1)

    # Verify notification was broadcast
    assert APPROVAL_POLICY_RESOURCE_URI in notifications_received


@pytest.mark.asyncio
async def test_approve_tool_broadcasts_notifications(agents_client_and_server, mock_approval_hub):
    """Test that approve_tool_call broadcasts approval resource notifications."""
    client, server = agents_client_and_server

    # Setup pending approval
    tool_call = ToolCall(name="test_tool", call_id="call-123", args_json="{}")
    add_pending_approval(mock_approval_hub, "call-123", tool_call)

    # Track notifications
    notifications_received = []

    original_broadcast = server.broadcast_resource_updated

    async def track_broadcast(uri: str):
        notifications_received.append(uri)
        await original_broadcast(uri)

    server.broadcast_resource_updated = track_broadcast

    # Call decide_approval tool with approve decision
    await client.call_tool("decide_approval", arguments={"agent_id": "test-agent", "call_id": "call-123", "decision": "approve"})

    # Give time for notifications
    await asyncio.sleep(0.1)

    # Verify notifications were broadcast
    expected_notifications = [
        "resource://agents/test-agent/approvals/pending",
        "resource://agents/test-agent/approvals/history",
        "resource://approvals/pending",
    ]

    for expected in expected_notifications:
        assert expected in notifications_received, f"Expected notification {expected} not found"


@pytest.mark.asyncio
async def test_reject_tool_broadcasts_notifications(agents_client_and_server, mock_approval_hub):
    """Test that reject_tool_call broadcasts approval resource notifications."""
    client, server = agents_client_and_server

    # Setup pending approval
    tool_call = ToolCall(name="test_tool", call_id="call-456", args_json="{}")
    add_pending_approval(mock_approval_hub, "call-456", tool_call)

    # Track notifications
    notifications_received = []

    original_broadcast = server.broadcast_resource_updated

    async def track_broadcast(uri: str):
        notifications_received.append(uri)
        await original_broadcast(uri)

    server.broadcast_resource_updated = track_broadcast

    # Call decide_approval tool with deny_continue decision
    await client.call_tool(
        "decide_approval", arguments={"agent_id": "test-agent", "call_id": "call-456", "decision": "deny_continue", "reason": "Test"}
    )

    # Give time for notifications
    await asyncio.sleep(0.1)

    # Verify notifications were broadcast
    expected_notifications = [
        "resource://agents/test-agent/approvals/pending",
        "resource://agents/test-agent/approvals/history",
        "resource://approvals/pending",
    ]

    for expected in expected_notifications:
        assert expected in notifications_received, f"Expected notification {expected} not found"


@pytest.mark.asyncio
async def test_multiple_agents_get_separate_notifiers(mock_registry, mock_approval_engine):
    """Test that each agent gets its own notifier with proper closure."""
    # Add second agent to registry
    second_engine = Mock()
    second_engine.persistence = Mock()
    second_engine.persistence.list_tool_calls = AsyncMock(return_value=[])
    second_engine.persistence.list_policy_proposals = AsyncMock(return_value=[])
    second_engine._notifier = None

    def set_notifier_second(notifier):
        second_engine._notifier = notifier

    second_engine.set_notifier = set_notifier_second

    second_infra = Mock()
    second_infra.approval_hub = ApprovalHub()
    second_infra.approval_engine = second_engine

    original_get_infrastructure = mock_registry.get_infrastructure
    original_known_agents = mock_registry.known_agents

    async def get_infrastructure_multi(agent_id: AgentID):
        if agent_id == "test-agent-2":
            return second_infra
        return await original_get_infrastructure(agent_id)

    def known_agents_multi():
        return [*original_known_agents(), "test-agent-2"]

    mock_registry.get_infrastructure = get_infrastructure_multi
    mock_registry.known_agents = known_agents_multi

    # Create server (should wire both agents)
    _server = await make_agents_server(mock_registry)

    # Both engines should have notifiers
    assert mock_approval_engine._notifier is not None
    assert second_engine._notifier is not None

    # Notifiers should be different instances
    assert mock_approval_engine._notifier is not second_engine._notifier
