from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import docker
import pytest
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig
from fastmcp.server import FastMCP

from agent_server.approvals import load_default_policy_source
from agent_server.mcp.approval_policy.engine import PolicyEngine
from agent_server.persist.sqlite import SQLitePersistence
from agent_server.persist.types import AgentMetadata
from agent_server.policies.loader import approve_all_policy_text
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest
from agent_server.policy_eval.container import ContainerPolicyEvaluator
from agent_server.runtime.container import AgentContainerCompositor
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.mcp_types import McpServerSpecs
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from test_util.docker import load_bazel_image, pytest_runtest_setup, python_slim_image  # noqa: F401

# Image tags and load scripts for Bazel-loaded images
RUNTIME_IMAGE_TAG = "adgn-runtime:latest"
RUNTIME_LOAD_SCRIPT = "agent_server/load.sh"

# Test server mount name used in fixtures
TEST_BACKEND_SERVER_NAME = "backend"

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core_testing.fixtures import *  # noqa: E402, F403
from agent_core_testing.responses import *  # noqa: E402, F403
from mcp_infra.testing.fixtures import *  # noqa: E402, F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


# --- Pytest fixtures ---


@pytest.fixture
def docker_client():
    """Sync docker client fixture."""
    return docker.from_env()


@pytest.fixture(scope="session")
def runtime_image():
    """Load agent server runtime image from Bazel :load target."""
    return load_bazel_image(RUNTIME_LOAD_SCRIPT, RUNTIME_IMAGE_TAG)


@pytest.fixture
async def sqlite_persistence(tmp_path):
    """Create isolated SQLite persistence in per-test tmpdir."""
    p = SQLitePersistence(tmp_path / "agent.sqlite")
    await p.ensure_schema()
    return p


@pytest.fixture
def test_agent_id(request: pytest.FixtureRequest) -> str:
    """Generate a sanitized agent ID from the test node ID."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", request.node.nodeid) or "tests"


@pytest.fixture
async def mock_registry(sqlite_persistence):
    """Create mock infrastructure registry using real persistence.

    Used by mcp_bridge and mcp/agents tests. Mocks agent container tracking
    while using real persistence for data storage.
    """
    registry = MagicMock()
    registry.persistence = sqlite_persistence
    registry.list_agents.return_value = []
    registry.is_external.return_value = False
    return registry


async def _mount_servers(comp: Compositor, servers: McpServerSpecs) -> None:
    """Mount all servers from McpServerSpecs dict onto a compositor.

    Validates that all servers are FastMCP instances and mounts them in-process.
    """
    for name, srv in servers.items():
        if not isinstance(srv, FastMCP):
            raise TypeError(f"invalid server for {name!r}: {type(srv).__name__}")
        await comp.mount_inproc(MCPMountPrefix(name), srv)


@pytest.fixture
async def approval_policy_server(sqlite_persistence, async_docker_client) -> PolicyEngine:
    """PolicyEngine fixture that owns .reader, .proposer and .approver sub-servers."""
    return PolicyEngine(
        agent_id="tests",
        persistence=sqlite_persistence,
        policy_source=load_default_policy_source(),
        docker_client=async_docker_client,
    )


@pytest.fixture
async def make_approval_policy_server(
    sqlite_persistence, test_agent_id, async_docker_client
) -> Callable[[str], Awaitable[PolicyEngine]]:
    """Factory producing PolicyEngine instances with per-test defaults."""

    async def _make(policy_source: str) -> PolicyEngine:
        # Create agent in DB first to satisfy FK constraints
        agent_id_resolved = await sqlite_persistence.create_agent(
            mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test")
        )
        return PolicyEngine(
            agent_id=agent_id_resolved,
            persistence=sqlite_persistence,
            policy_source=policy_source,
            docker_client=async_docker_client,
        )

    return _make


@pytest.fixture
async def policy_evaluator(async_docker_client, approval_policy_server: PolicyEngine) -> ContainerPolicyEvaluator:
    """Container-backed policy evaluator using the default policy engine.

    Deduplicates setup across tests that need to call policy.decide(...).
    Requires Docker (tests should mark with @pytest.mark.requires_docker).
    """
    return ContainerPolicyEvaluator(agent_id="tests", docker_client=async_docker_client, engine=approval_policy_server)


# ---- Standard policy text fixtures (string sources) ----


@pytest.fixture
def policy_allow_all() -> str:
    """Return the text of the approve-all policy from packaged resources."""
    return approve_all_policy_text()


# ---- PolicyRequest test helper ----


def make_policy_request(server: MCPMountPrefix, tool: str, arguments: dict[str, Any] | None = None) -> PolicyRequest:
    """Helper to create PolicyRequest instances for tests.

    Args:
        server: MCP mount prefix (validated)
        tool: Tool name
        arguments: Tool arguments dict (will be JSON-encoded). Defaults to empty dict.

    Returns:
        PolicyRequest with arguments JSON-encoded as string.
    """
    return PolicyRequest(
        name=build_mcp_function(server, tool), arguments_json=json.dumps(arguments) if arguments is not None else None
    )


# --- Policy gateway fixtures (for MCP middleware tests) ---


async def _create_test_policy_engine(sqlite_persistence, async_docker_client) -> PolicyEngine:
    """Create a test policy engine with approve-all policy."""
    agent_id_resolved = await sqlite_persistence.create_agent(
        mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test")
    )
    return PolicyEngine(
        agent_id=agent_id_resolved,
        persistence=sqlite_persistence,
        policy_source=approve_all_policy_text(),
        docker_client=async_docker_client,
    )


@pytest.fixture
async def _setup_policy_gateway_compositor(sqlite_persistence, async_docker_client, test_agent_id):
    """Fixture factory for AgentContainerCompositor with policy gateway middleware."""

    @asynccontextmanager
    async def _factory(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        if policy_engine is None:
            policy_engine = await _create_test_policy_engine(sqlite_persistence, async_docker_client)

        comp = AgentContainerCompositor(
            approval_engine=policy_engine,
            ui_bus=None,
            async_docker_client=async_docker_client,
            persistence=sqlite_persistence,
            agent_id=test_agent_id,
        )
        async with comp:
            comp.add_middleware(policy_engine.gateway)
            await _mount_servers(comp, servers)
            yield comp

    return _factory


@pytest.fixture
async def make_policy_gateway_client(_setup_policy_gateway_compositor):
    """Async helper to open an AgentContainerCompositor with policy gateway, yielding just the client."""

    @asynccontextmanager
    async def _open(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        async with _setup_policy_gateway_compositor(servers, policy_engine=policy_engine) as comp, Client(comp) as sess:
            yield sess

    return _open


@pytest.fixture
async def make_policy_gateway_compositor(_setup_policy_gateway_compositor):
    """Async helper factory yielding typed AgentContainerCompositor."""

    @asynccontextmanager
    async def _open(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        async with _setup_policy_gateway_compositor(servers, policy_engine=policy_engine) as comp:
            yield comp

    return _open


@pytest.fixture
async def policy_gateway_client(make_policy_gateway_client, make_simple_mcp):
    """Ready-to-use client with make_simple_mcp mounted and allow-all policy."""
    async with make_policy_gateway_client({TEST_BACKEND_SERVER_NAME: make_simple_mcp}) as sess:
        yield sess


def make_policy_source(decision: ApprovalDecision) -> str:
    """Generate a policy source that always returns the specified decision."""
    return f'''"""Policy that returns {decision.value} for all calls."""
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from agent_server.policies.scaffold import run

def decide(_req: PolicyRequest) -> PolicyResponse:
    return PolicyResponse(decision=ApprovalDecision.{decision.name}, rationale="{decision.value}")

if __name__ == "__main__":
    raise SystemExit(run(decide))
'''


@pytest.fixture
async def make_decision_engine(
    make_approval_policy_server: Callable[[str], Awaitable[PolicyEngine]],
) -> Callable[[ApprovalDecision], Awaitable[PolicyEngine]]:
    """Factory for creating PolicyEngine with a specific decision policy."""

    async def _make(decision: ApprovalDecision) -> PolicyEngine:
        policy_source = make_policy_source(decision)
        return await make_approval_policy_server(policy_source)

    return _make
