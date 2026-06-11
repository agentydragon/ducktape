"""Integration tests for mcp_bridge module.

Tests the Phase 5 two-compositor architecture components:
- InfrastructureRegistry
- TokenRoutingASGI
- agents management server
- agent_control server
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.mcp_config import MCPConfig
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_server.mcp_bridge.agents import AgentsManagementServer
from agent_server.mcp_bridge.auth import TokenRoutingASGI, TokensConfig
from agent_server.mcp_bridge.registry import InfrastructureRegistry

# ---------------------------------------------------------------------------
# Shared Fixtures
# ---------------------------------------------------------------------------
# Note: sqlite_persistence and docker_client fixtures come from tests/conftest.py
# Note: mock_registry fixture comes from tests/mcp/conftest.py


@pytest.fixture
def mock_compositor():
    """Create mock compositor for verifying mount/unmount calls."""
    compositor = AsyncMock()
    compositor.mount_inproc = AsyncMock()
    compositor.unmount_server = AsyncMock()
    return compositor


@pytest.fixture
def mock_container():
    """Create mock agent container for verifying close calls."""
    container = MagicMock()
    container.agent_id = "testagent1"
    container._compositor = MagicMock()
    container.session = None  # For agent_control tests
    container.close = AsyncMock()

    # Real make_control_server for testing
    def _make_control_server(name: str) -> FastMCP:
        mcp = FastMCP(name)

        @mcp.tool()
        async def send_prompt(prompt: str) -> dict:
            if container.session is None:
                return {"status": "error", "message": "Agent session not initialized"}
            return {"status": "started", "message": "Prompt sent successfully"}

        @mcp.tool()
        async def abort_run() -> dict:
            if container.session is None:
                return {"status": "error", "message": "Agent session not initialized"}
            return {"status": "aborted", "message": "Run aborted successfully"}

        return mcp

    container.make_control_server = _make_control_server
    return container


@pytest.fixture
def user_app():
    """Create a simple user-facing ASGI app for routing tests."""

    async def homepage(request):
        return PlainTextResponse("user-app")

    return Starlette(routes=[Route("/", homepage)])


@pytest.fixture
def agent_app_factory():
    """Factory to create agent ASGI apps that identify themselves."""

    def make_app(agent_id: str):
        async def homepage(request):
            return PlainTextResponse(f"agent-{agent_id}")

        return Starlette(routes=[Route("/", homepage)])

    return make_app


@pytest.fixture
def make_token_router(user_app, agent_app_factory):
    """Factory to create TokenRoutingASGI with customizable tokens and apps.

    Default configuration:
    - Single user token: "user-token" -> "admin"
    - Empty agent tokens and apps

    Usage:
        router = make_token_router()  # defaults
        router = make_token_router(agent_tokens={"tok": "agent1"}, agent_ids=["1"])
    """

    def _make(
        *,
        user_tokens: dict[str, str] | None = None,
        agent_tokens: dict[str, str] | None = None,
        agent_ids: list[str] | None = None,
    ) -> TokenRoutingASGI:
        return TokenRoutingASGI(
            user_tokens=user_tokens or {"user-token": "admin"},
            agent_tokens=agent_tokens or {},
            user_app=user_app,
            agent_apps={aid: agent_app_factory(aid) for aid in (agent_ids or [])},
        )

    return _make


@pytest.fixture
async def registry(sqlite_persistence):
    """InfrastructureRegistry with real persistence and mock Docker clients."""
    return InfrastructureRegistry(
        persistence=sqlite_persistence,
        model="test-model",
        client_factory=lambda m: MagicMock(),
        async_docker_client=MagicMock(),
        mcp_config=MCPConfig(mcpServers={}),
    )


# ---------------------------------------------------------------------------
# Token Loading Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tokens_yaml(tmp_path: Path):
    """Factory fixture to write tokens YAML and return the path."""
    path = tmp_path / "tokens.yaml"

    def _write(content: str) -> Path:
        path.write_text(content)
        return path

    return _write


class TestTokensConfig:
    """Tests for TokensConfig loading."""

    def test_missing_file(self):
        """Returns empty tokens when config file doesn't exist."""
        config = TokensConfig.from_yaml_file(Path("/nonexistent/path.yaml"))
        assert config.user_tokens() == {}
        assert config.agent_tokens() == {}

    def test_from_yaml_file(self, tokens_yaml):
        """Loads tokens from YAML file."""
        path = tokens_yaml("""
users:
  admin: "admin-token-123"
  viewer: "viewer-token-456"

agents:
  claudecode1: "agent-token-aaa"
  externalagent: "agent-token-bbb"
""")
        config = TokensConfig.from_yaml_file(path)

        # User tokens: token -> user_id
        assert config.user_tokens() == {"admin-token-123": "admin", "viewer-token-456": "viewer"}

        # Agent tokens: token -> agent_id
        assert config.agent_tokens() == {"agent-token-aaa": "claudecode1", "agent-token-bbb": "externalagent"}

    def test_empty_file(self, tokens_yaml):
        """Returns empty tokens when config file is empty."""
        config = TokensConfig.from_yaml_file(tokens_yaml(""))
        assert config.user_tokens() == {}
        assert config.agent_tokens() == {}

    def test_null_values_skipped(self, tokens_yaml):
        """Skips null token values in config."""
        config = TokensConfig.from_yaml_file(
            tokens_yaml("""
users:
  admin: "valid-token"
  invalid: null
  empty: ""
""")
        )
        # Only non-null, non-empty tokens should be included
        assert config.user_tokens() == {"valid-token": "admin"}
        assert config.agent_tokens() == {}

    def test_model_validation(self, tokens_yaml):
        """TokensConfig validates and parses YAML correctly."""
        config = TokensConfig.from_yaml_file(
            tokens_yaml("""
users:
  admin: "token-1"
agents:
  agent1: "token-2"
""")
        )

        # Check raw model attributes
        assert config.users == {"admin": "token-1"}
        assert config.agents == {"agent1": "token-2"}


# ---------------------------------------------------------------------------
# Token Routing Tests
# ---------------------------------------------------------------------------


class TestTokenRoutingASGI:
    """Tests for TokenRoutingASGI ASGI router."""

    def test_routes_user_token_to_user_app(self, make_token_router):
        """User tokens route to user compositor app."""
        router = make_token_router(
            user_tokens={"user-token-123": "admin"}, agent_tokens={"agent-token-abc": "agent1"}, agent_ids=["agent1"]
        )

        client = TestClient(router)
        response = client.get("/", headers={"Authorization": "Bearer user-token-123"})
        assert response.status_code == 200
        assert response.text == "user-app"

    def test_routes_agent_token_to_agent_app(self, make_token_router):
        """Agent tokens route to their specific agent compositor app."""
        router = make_token_router(
            user_tokens={"user-token-123": "admin"},
            agent_tokens={"agent-token-abc": "agent1", "agent-token-xyz": "agent2"},
            agent_ids=["agent1", "agent2"],
        )

        client = TestClient(router)

        # First agent
        response = client.get("/", headers={"Authorization": "Bearer agent-token-abc"})
        assert response.status_code == 200
        assert response.text == "agent-agent1"

        # Second agent
        response = client.get("/", headers={"Authorization": "Bearer agent-token-xyz"})
        assert response.status_code == 200
        assert response.text == "agent-agent2"

    def test_returns_401_without_token(self, make_token_router):
        """Returns 401 when no Authorization header is present."""
        router = make_token_router()

        client = TestClient(router, raise_server_exceptions=False)
        response = client.get("/")
        assert response.status_code == 401
        assert "Bearer token required" in response.text

    def test_returns_401_without_bearer_prefix(self, make_token_router):
        """Returns 401 when Authorization header doesn't use Bearer scheme."""
        router = make_token_router()

        client = TestClient(router, raise_server_exceptions=False)
        response = client.get("/", headers={"Authorization": "Basic token"})
        assert response.status_code == 401
        assert "Bearer token required" in response.text

    def test_returns_401_for_invalid_token(self, make_token_router):
        """Returns 401 when token is not recognized."""
        router = make_token_router(user_tokens={"valid-token": "user"})

        client = TestClient(router, raise_server_exceptions=False)
        response = client.get("/", headers={"Authorization": "Bearer invalid-token"})
        assert response.status_code == 401
        assert "Invalid token" in response.text

    def test_returns_404_when_agent_app_not_found(self, make_token_router):
        """Returns 404 when agent token is valid but agent app isn't registered."""
        router = make_token_router(
            user_tokens={},
            agent_tokens={"agent-token": "agent1"},
            agent_ids=[],  # No agent apps registered
        )

        client = TestClient(router, raise_server_exceptions=False)
        response = client.get("/", headers={"Authorization": "Bearer agent-token"})
        assert response.status_code == 404
        assert "Agent not found" in response.text


# ---------------------------------------------------------------------------
# MCP Server Creation Tests
# ---------------------------------------------------------------------------


class TestAgentsServer:
    """Tests for agents management server."""

    async def test_agents_management_server_creates_server(self, mock_registry):
        """AgentsManagementServer creates a FastMCP server."""
        server = AgentsManagementServer(mock_registry)

        assert server is not None


class TestAgentControlServer:
    """Tests for agent control server via AgentContainer.make_control_server()."""

    async def test_container_make_control_server_creates_server(self, mock_container):
        """container.make_control_server() creates a FastMCP server."""
        server = mock_container.make_control_server("test-control")

        assert server is not None
        assert server.name == "test-control"


# ---------------------------------------------------------------------------
# Infrastructure Registry Tests
# ---------------------------------------------------------------------------


class TestInfrastructureRegistry:
    """Tests for InfrastructureRegistry agent lifecycle management."""

    async def test_get_agent_returns_none_for_unknown(self, registry):
        """get_agent returns None for unknown agent."""
        assert registry.get_agent("unknown-id") is None

    async def test_list_agents_empty_initially(self, registry):
        """list_agents returns empty list initially."""
        assert registry.list_agents() == []

    async def test_is_external_false_for_unknown(self, registry):
        """is_external returns False for unknown agent."""
        assert registry.is_external("unknown-id") is False

    async def test_shutdown_agent_raises_for_unknown(self, registry, mock_compositor):
        """shutdown_agent raises KeyError for unknown agent."""
        registry.global_compositor = mock_compositor

        with pytest.raises(KeyError, match="Agent not running"):
            await registry.shutdown_agent("unknown-id")

    async def test_shutdown_agent_raises_without_compositor(self, registry):
        """shutdown_agent raises RuntimeError without global compositor."""
        with pytest.raises(RuntimeError, match=r"(?i)global compositor not initialized"):
            await registry.shutdown_agent("unknown-id")

    async def test_shutdown_agent_closes_container(self, registry, mock_container, mock_compositor):
        """shutdown_agent closes container and unmounts from compositor."""
        registry.global_compositor = mock_compositor
        registry._agents["testagent1"] = mock_container

        await registry.shutdown_agent("testagent1")

        mock_container.close.assert_awaited_once()
        # Agent mount prefix is "agent_" + agent_id
        mock_compositor.unmount_server.assert_awaited_once_with("agent_testagent1")
        assert "testagent1" not in registry._agents

    async def test_shutdown_agent_cleans_up_external_tracking(self, registry, mock_container, mock_compositor):
        """shutdown_agent removes agent from external tracking set."""
        registry.global_compositor = mock_compositor
        registry._agents["testagent1"] = mock_container
        registry._external_agents.add("testagent1")

        assert registry.is_external("testagent1") is True

        await registry.shutdown_agent("testagent1")

        assert registry.is_external("testagent1") is False

    async def test_shutdown_all_shuts_down_all_agents(self, registry, mock_compositor):
        """shutdown_all shuts down all registered agents."""
        registry.global_compositor = mock_compositor

        # Add multiple mock containers
        container1 = MagicMock()
        container1.agent_id = "agent1"
        container1._compositor = MagicMock()
        container1.close = AsyncMock()

        container2 = MagicMock()
        container2.agent_id = "agent2"
        container2._compositor = MagicMock()
        container2.close = AsyncMock()

        registry._agents["agent1"] = container1
        registry._agents["agent2"] = container2

        await registry.shutdown_all()

        container1.close.assert_awaited_once()
        container2.close.assert_awaited_once()
        assert len(registry._agents) == 0

    async def test_boot_agent_returns_existing_if_already_booted(self, registry, mock_container, mock_compositor):
        """boot_agent returns existing container if already booted."""
        registry.global_compositor = mock_compositor
        registry._agents["testagent1"] = mock_container

        result = await registry.boot_agent("testagent1")

        assert result is mock_container

    async def test_boot_agent_raises_for_unknown_agent(self, registry, mock_compositor):
        """boot_agent raises KeyError if agent not in DB (using real persistence)."""
        registry.global_compositor = mock_compositor
        with pytest.raises(KeyError, match="Agent not found"):
            await registry.boot_agent("nonexistentagent")

    async def test_boot_agent_raises_without_compositor(self, registry):
        """boot_agent raises RuntimeError without global compositor."""
        with pytest.raises(RuntimeError, match=r"(?i)global compositor not initialized"):
            await registry.boot_agent("nonexistentagent")

    async def test_create_external_agent_returns_existing_if_already_created(
        self, registry, mock_container, mock_compositor
    ):
        """create_external_agent returns existing container if already exists."""
        registry.global_compositor = mock_compositor
        registry._agents["testagent1"] = mock_container

        result = await registry.create_external_agent("testagent1")

        assert result is mock_container

    async def test_create_external_agent_raises_without_compositor(self, registry):
        """create_external_agent raises RuntimeError without global compositor."""
        with pytest.raises(RuntimeError, match=r"(?i)global compositor not initialized"):
            await registry.create_external_agent("testagent1")

    async def test_create_external_agent_marks_as_external(
        self, registry, mock_compositor, monkeypatch: pytest.MonkeyPatch
    ):
        """create_external_agent marks agent as external."""
        registry.global_compositor = mock_compositor

        # Patch build_container to return a mock
        container = MagicMock()
        container.agent_id = "externalagent"
        container._compositor = MagicMock()
        monkeypatch.setattr("agent_server.mcp_bridge.registry.build_container", AsyncMock(return_value=container))

        await registry.create_external_agent("externalagent")

        assert registry.is_external("externalagent") is True
        assert "externalagent" in registry._agents
