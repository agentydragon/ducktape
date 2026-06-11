"""MCP tests for agents management server.

Tests agents://list resource and agent lifecycle via MCP tools.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig

from agent_server.mcp_bridge.agents import AgentInfo, AgentsManagementServer, PresetInfo
from agent_server.persist.types import AgentMetadata
from mcp_infra.resource_utils import read_text_json_typed

# Note: mock_registry fixture comes from tests/mcp/conftest.py


@pytest.fixture
async def agents_server(mock_registry):
    """Create agents MCP server with mock registry."""
    return AgentsManagementServer(mock_registry)


class TestAgentsListResource:
    """Tests for agents://list resource."""

    async def test_list_agents_empty(self, agents_server, mock_registry):
        """agents://list returns empty list when no agents."""
        mock_registry.list_agents.return_value = []

        async with Client(agents_server) as sess:
            resources = await sess.list_resources()
            # Should have agents://list and agents://presets
            uris = [str(r.uri) for r in resources]
            assert str(agents_server.list_resource.uri) in uris
            assert str(agents_server.presets_resource.uri) in uris

            # Read the list resource and parse as list of AgentInfo
            agents: list[AgentInfo] = await read_text_json_typed(sess, agents_server.list_resource.uri, list[AgentInfo])
            assert len(agents) == 0

    async def test_list_agents_with_running_agent(self, agents_server, mock_registry, sqlite_persistence):
        """agents://list returns running agent info."""
        # Create agent record in persistence first
        agent_id = await sqlite_persistence.create_agent(
            mcp_config=MCPConfig(mcpServers={}), metadata=AgentMetadata(preset="default")
        )

        # Setup mock container
        mock_container = MagicMock()
        mock_container.agent_id = agent_id
        mock_registry.list_agents.return_value = [mock_container]
        mock_registry.is_external.return_value = False

        async with Client(agents_server) as sess:
            agents: list[AgentInfo] = await read_text_json_typed(sess, agents_server.list_resource.uri, list[AgentInfo])
            assert len(agents) == 1
            agent = agents[0]
            assert agent.id == agent_id
            assert agent.preset == "default"
            assert agent.booted is True
            assert agent.external is False

    async def test_list_agents_shows_persisted_not_booted(self, agents_server, mock_registry, sqlite_persistence):
        """agents://list shows persisted agents that aren't booted."""
        mock_registry.list_agents.return_value = []

        # Create agent record in persistence
        agent_id = await sqlite_persistence.create_agent(
            mcp_config=MCPConfig(mcpServers={}), metadata=AgentMetadata(preset="my-preset")
        )

        async with Client(agents_server) as sess:
            agents: list[AgentInfo] = await read_text_json_typed(sess, agents_server.list_resource.uri, list[AgentInfo])
            assert len(agents) == 1
            agent = agents[0]
            assert agent.id == agent_id
            assert agent.preset == "my-preset"
            assert agent.booted is False

    async def test_list_agents_external_flag(self, agents_server, mock_registry, sqlite_persistence):
        """agents://list correctly marks external agents."""
        # Create agent record in persistence
        agent_id = await sqlite_persistence.create_agent(
            mcp_config=MCPConfig(mcpServers={}), metadata=AgentMetadata(preset="external")
        )

        mock_container = MagicMock()
        mock_container.agent_id = agent_id
        mock_registry.list_agents.return_value = [mock_container]
        mock_registry.is_external.return_value = True

        async with Client(agents_server) as sess:
            agents: list[AgentInfo] = await read_text_json_typed(sess, agents_server.list_resource.uri, list[AgentInfo])
            assert len(agents) == 1
            agent = agents[0]
            assert agent.id == agent_id
            assert agent.external is True


class TestAgentsPresetsResource:
    """Tests for agents://presets resource."""

    async def test_list_presets(self, agents_server):
        """agents://presets returns available presets."""
        async with Client(agents_server) as sess:
            presets: list[PresetInfo] = await read_text_json_typed(
                sess, agents_server.presets_resource.uri, list[PresetInfo]
            )
            assert isinstance(presets, list)
            # Should have at least the default preset
            names = [p.name for p in presets]
            assert "default" in names
