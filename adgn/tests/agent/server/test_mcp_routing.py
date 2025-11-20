"""Tests for token-based MCP connection routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from adgn.agent.server.app import create_app
from adgn.agent.server.mcp_routing import AgentTokenInfo, HumanTokenInfo, TOKEN_TABLE, TokenInfo, TokenRole
from adgn.agent.types import AgentID


@pytest.fixture
def test_tokens() -> dict[str, TokenInfo]:
    """Override the global TOKEN_TABLE for testing."""
    return {
        "test-human-token": HumanTokenInfo(role=TokenRole.HUMAN),
        "test-agent-token": AgentTokenInfo(role=TokenRole.AGENT, agent_id=AgentID("test-agent-1")),
    }


@pytest.fixture
def mock_registry():
    """Mock AgentRegistry for testing."""
    registry = MagicMock()
    # Mock ensure_live to return a container with compositor
    mock_container = MagicMock()
    mock_compositor = MagicMock()
    mock_http_app = MagicMock()
    mock_http_app.return_value = AsyncMock()
    mock_compositor.http_app = mock_http_app
    mock_container.running.compositor = mock_compositor

    async def mock_ensure_live(agent_id, with_ui=False):
        return mock_container

    registry.ensure_live = AsyncMock(side_effect=mock_ensure_live)
    return registry


@pytest.fixture
def mock_agents_server():
    """Mock agents management server."""
    server = MagicMock()
    mock_http_app = MagicMock()
    server.http_app.return_value = mock_http_app
    return server


class TestMCPRouting:
    """Tests for token-based MCP routing middleware."""

    @pytest.mark.asyncio
    async def test_missing_authorization_header(self):
        """Test that requests without Authorization header return 401."""
        app = create_app(require_static_assets=False)
        await app.router.startup()

        client = TestClient(app)
        response = client.post("/mcp/message")

        assert response.status_code == 401
        assert "Missing Authorization header" in response.text

    @pytest.mark.asyncio
    async def test_invalid_token(self, test_tokens):
        """Test that requests with invalid token return 401."""
        app = create_app(require_static_assets=False)
        await app.router.startup()

        with patch("adgn.agent.server.mcp_routing.TOKEN_TABLE", test_tokens):
            client = TestClient(app)
            response = client.post("/mcp/message", headers={"Authorization": "Bearer invalid-token-xyz"})

            assert response.status_code == 401
            assert "Invalid token" in response.text

    @pytest.mark.asyncio
    async def test_human_token_routes_to_agents_server(self, test_tokens, mock_registry, mock_agents_server):
        """Test that human token routes to agents management server."""
        app = create_app(require_static_assets=False)
        app.state.registry = mock_registry
        app.state.agents_server = mock_agents_server
        await app.router.startup()

        with patch("adgn.agent.server.mcp_routing.TOKEN_TABLE", test_tokens):
            client = TestClient(app)
            client.post("/mcp/message", headers={"Authorization": "Bearer test-human-token"}, json={"type": "test"})

            # The agents server's http_app should have been called
            assert mock_agents_server.http_app.called

    @pytest.mark.asyncio
    async def test_agent_token_routes_to_compositor(self, test_tokens, mock_registry, mock_agents_server):
        """Test that agent token routes to agent's compositor."""
        app = create_app(require_static_assets=False)
        app.state.registry = mock_registry
        app.state.agents_server = mock_agents_server
        await app.router.startup()

        with patch("adgn.agent.server.mcp_routing.TOKEN_TABLE", test_tokens):
            client = TestClient(app)
            client.post("/mcp/message", headers={"Authorization": "Bearer test-agent-token"}, json={"type": "test"})

            # The registry should have been called to get the agent
            mock_registry.ensure_live.assert_called_once_with("test-agent-1", with_ui=False)

    @pytest.mark.asyncio
    async def test_invalid_role_returns_500(self, test_tokens, mock_registry, mock_agents_server):
        """Test that token with invalid role returns 500."""
        app = create_app(require_static_assets=False)
        app.state.registry = mock_registry
        app.state.agents_server = mock_agents_server
        await app.router.startup()

        with patch("adgn.agent.server.mcp_routing.TOKEN_TABLE", test_tokens):
            client = TestClient(app)
            response = client.post(
                "/mcp/message", headers={"Authorization": "Bearer test-invalid-role"}, json={"type": "test"}
            )

            assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_multiple_connections_same_token(self, test_tokens, mock_registry, mock_agents_server):
        """Test that multiple connections with same token reuse cached backend."""
        app = create_app(require_static_assets=False)
        app.state.registry = mock_registry
        app.state.agents_server = mock_agents_server
        await app.router.startup()

        with patch("adgn.agent.server.mcp_routing.TOKEN_TABLE", test_tokens):
            client = TestClient(app)

            # First request
            client.post("/mcp/message", headers={"Authorization": "Bearer test-human-token"}, json={"type": "test1"})

            # Second request with same token
            client.post("/mcp/message", headers={"Authorization": "Bearer test-human-token"}, json={"type": "test2"})

            # http_app should only be called once (cached)
            assert mock_agents_server.http_app.call_count == 1

    @pytest.mark.asyncio
    async def test_token_role_enum(self):
        """Test TokenRole enum values."""
        # Test that enum can be created from string
        role = TokenRole("human")
        assert role == TokenRole.HUMAN

    @pytest.mark.asyncio
    async def test_agent_token_without_agent_id_returns_500(self, mock_registry, mock_agents_server):
        """Test that agent token without agent_id returns 500."""
        # Create a token table with agent role but no agent_id
        bad_tokens = {
            "bad-agent-token": {"role": "agent"}  # Missing agent_id
        }

        app = create_app(require_static_assets=False)
        app.state.registry = mock_registry
        app.state.agents_server = mock_agents_server
        await app.router.startup()

        with patch("adgn.agent.server.mcp_routing.TOKEN_TABLE", bad_tokens):
            client = TestClient(app)
            response = client.post(
                "/mcp/message", headers={"Authorization": "Bearer bad-agent-token"}, json={"type": "test"}
            )

            assert response.status_code == 500
            assert "requires agent_id" in response.text


class TestTokenTable:
    """Tests for the token table structure."""

    def test_default_token_table_structure(self):
        """Test that the default TOKEN_TABLE has expected structure."""
        assert "human-token-123" in TOKEN_TABLE
        assert TOKEN_TABLE["human-token-123"]["role"] == "human"

        assert "agent-token-abc" in TOKEN_TABLE
        assert TOKEN_TABLE["agent-token-abc"]["role"] == "agent"
        assert TOKEN_TABLE["agent-token-abc"]["agent_id"] == "agent-1"

    def test_human_token_no_agent_id(self):
        """Test that human tokens don't require agent_id."""
        human_token = TOKEN_TABLE["human-token-123"]
        assert "agent_id" not in human_token

    def test_agent_token_has_agent_id(self):
        """Test that agent tokens include agent_id."""
        agent_token = TOKEN_TABLE["agent-token-abc"]
        assert "agent_id" in agent_token
        assert isinstance(agent_token["agent_id"], str)
