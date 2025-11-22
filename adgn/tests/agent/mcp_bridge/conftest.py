"""Shared fixtures for MCP Bridge tests.

Consolidates duplicated test infrastructure used across multiple test modules
to reduce maintenance burden and ensure consistency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from adgn.agent.approvals import ApprovalHub
from adgn.agent.mcp_bridge.auth import UITokenAuthMiddleware
from adgn.agent.mcp_bridge.types import AgentID, AgentMode
from adgn.mcp.snapshots import SamplingSnapshot

# Shared test agent ID (for single-agent tests; multiagent tests can create their own)
TEST_AGENT = AgentID("test-agent")


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create temporary database path.

    Shared by all MCP bridge tests that need a temporary database.
    Usage: any test function with temp_db parameter will receive a Path object.
    """
    return tmp_path / "test.db"


@pytest.fixture
def mock_persistence():
    """Mock persistence with tool call records and policy proposals.

    Shared by approval engine and infrastructure tests.
    Provides empty lists by default; tests can configure return values as needed.
    """
    persistence = Mock()

    # Default empty lists
    persistence.list_tool_calls = AsyncMock(return_value=[])
    persistence.list_policy_proposals = AsyncMock(return_value=[])

    return persistence


@pytest.fixture
def mock_approval_hub():
    """Mock ApprovalHub with pending approvals.

    Shared by all infrastructure tests. Just use a real ApprovalHub - it's simple enough.
    """
    return ApprovalHub()


@pytest.fixture
def mock_approval_engine(mock_persistence):
    """Simple fake approval engine for testing.

    Uses a simple object with the minimal interface needed, not a Mock.
    """
    from adgn.agent.approvals import load_default_policy_source

    class FakeApprovalEngine:
        """Minimal fake for ApprovalPolicyEngine."""

        def __init__(self, persistence):
            self.persistence = persistence
            self._policy_source = load_default_policy_source()
            self._policy_id = 1
            self._notifier = None

        def set_notifier(self, notifier):
            self._notifier = notifier

        def get_policy(self):
            return (self._policy_source, self._policy_id)

        async def set_policy(self, source: str) -> int:
            self._policy_source = source
            self._policy_id += 1
            if self._notifier:
                self._notifier("resource://approval-policy/policy.py")
            return self._policy_id

    return FakeApprovalEngine(mock_persistence)


@pytest.fixture
def mock_running_infrastructure(mock_approval_hub, mock_approval_engine):
    """Real RunningInfrastructure for testing.

    Uses real objects wherever possible instead of mocks for better xdist compatibility.
    """
    from contextlib import AsyncExitStack
    from adgn.agent.runtime.running import RunningInfrastructure
    from adgn.mcp.compositor.server import Compositor
    from adgn.mcp.notifications.buffer import NotificationsBuffer

    # Create real infrastructure components
    compositor = Compositor("test-compositor")

    # Mock only the complex client objects
    compositor_client = Mock()
    policy_reader = Mock()
    policy_approver = Mock()

    # Create notifications buffer with compositor
    notifications_buffer = NotificationsBuffer(compositor=compositor)

    # Create real RunningInfrastructure
    infra = RunningInfrastructure(
        compositor=compositor,
        compositor_client=compositor_client,
        notifications_buffer=notifications_buffer,
        policy_reader=policy_reader,
        policy_approver=policy_approver,
        approval_engine=mock_approval_engine,
        approval_hub=mock_approval_hub,
        agent_id=AgentID("test-agent"),
        _stack=AsyncExitStack(),
    )

    return infra


@pytest.fixture
def mock_local_runtime():
    """Mock LocalAgentRuntime with agent.

    Shared by tests that need a local runtime with abort capability.
    Only used when testing local agent modes.
    """
    from adgn.agent.server.state import new_state

    runtime = Mock()
    runtime.agent = Mock()
    runtime.agent.abort = AsyncMock()

    # Mock running infrastructure with compositor
    runtime.running = Mock()
    runtime.running.compositor = Mock()
    runtime.running.compositor.sampling_snapshot = AsyncMock(
        return_value=SamplingSnapshot(ts="2025-01-15T10:00:00Z", servers={})
    )

    # Mock session with ui_state
    runtime.session = Mock()
    runtime.session.ui_state = new_state()

    return runtime


@pytest.fixture
def mock_registry(mock_running_infrastructure, mock_local_runtime) -> Mock:
    """Mock InfrastructureRegistry with agents.

    Shared by all agent server tests. Provides a realistic agent registry
    with both local and bridge agents.

    Two agents are available by default:
    - "local-agent": LOCAL mode with full infrastructure and runtime
    - "bridge-agent": BRIDGE mode with infrastructure but no runtime
    """
    registry = Mock()

    # Track registered agents
    agents: dict[AgentID, dict[str, Any]] = {
        AgentID("local-agent"): {
            "mode": AgentMode.LOCAL,
            "infrastructure": mock_running_infrastructure,
            "runtime": mock_local_runtime,
        },
        AgentID("bridge-agent"): {
            "mode": AgentMode.BRIDGE,
            "infrastructure": mock_running_infrastructure,
            "runtime": None,
        },
    }

    def known_agents():
        return list(agents.keys())

    def get_agent_mode(agent_id: AgentID) -> AgentMode:
        if agent_id not in agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        mode: AgentMode = agents[agent_id]["mode"]
        return mode

    async def get_infrastructure(agent_id: AgentID):
        if agent_id not in agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        infra = agents[agent_id]["infrastructure"]
        if infra is None:
            raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
        return infra

    def get_running_infrastructure(agent_id: AgentID):
        """Get running infrastructure if it exists (doesn't create)."""
        if agent_id not in agents:
            return None
        return agents[agent_id]["infrastructure"]

    def get_local_runtime(agent_id: AgentID):
        if agent_id not in agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        return agents[agent_id]["runtime"]

    def get(agent_id: AgentID):
        """Get agent runtime (for UI state access)."""
        if agent_id not in agents:
            return None
        runtime_mock = Mock()
        runtime_mock.runtime = agents[agent_id]["runtime"]
        runtime_mock.running = agents[agent_id]["infrastructure"]
        return runtime_mock

    registry.known_agents = known_agents
    registry.get_agent_mode = get_agent_mode
    registry.get_infrastructure = get_infrastructure
    registry.get_running_infrastructure = get_running_infrastructure
    registry.get_local_runtime = get_local_runtime
    registry.get = get

    return registry


@pytest.fixture
def mock_registry_single_agent(mock_running_infrastructure) -> Mock:
    """Mock InfrastructureRegistry with single test agent.

    Alternative to mock_registry for tests that only need one agent.
    Provides TEST_AGENT in LOCAL mode.
    """
    registry = Mock()

    agents: dict[AgentID, dict[str, Any]] = {
        TEST_AGENT: {"mode": AgentMode.LOCAL, "infrastructure": mock_running_infrastructure}
    }

    def known_agents():
        return list(agents.keys())

    def get_agent_mode(agent_id: AgentID) -> AgentMode:
        if agent_id not in agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        mode: AgentMode = agents[agent_id]["mode"]
        return mode

    async def get_infrastructure(agent_id: AgentID):
        if agent_id not in agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        infra = agents[agent_id]["infrastructure"]
        if infra is None:
            raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
        return infra

    def get_local_runtime(agent_id: AgentID):
        if agent_id not in agents:
            raise KeyError(f"Agent {agent_id} not found in registry")

    registry.known_agents = known_agents
    registry.get_agent_mode = get_agent_mode
    registry.get_infrastructure = get_infrastructure
    registry.get_local_runtime = get_local_runtime

    return registry


@pytest.fixture
def auth_test_app_factory():
    """Factory to create FastAPI app with UITokenAuthMiddleware for testing.

    Shared by all UI auth tests to reduce duplication of app setup boilerplate.
    Each test creates the same app+middleware+endpoint pattern.

    Usage:
        app, client = auth_test_app_factory(expected_token="my-token")
        response = client.get("/test", headers={"Authorization": "Bearer my-token"})
    """

    def _create(expected_token: str) -> tuple[FastAPI, TestClient]:
        """Create app with auth middleware and test endpoint."""
        app = FastAPI()
        app.add_middleware(UITokenAuthMiddleware, expected_token=expected_token)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app, raise_server_exceptions=False)
        return app, client

    return _create
