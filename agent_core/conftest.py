"""Pytest configuration for agent_core tests."""

from __future__ import annotations

import pytest

from agent_core.agent import Agent
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.tool_provider import TextContent

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
# - agent_core_testing.fixtures: Core agent fixtures (recording_handler, make_test_agent, etc.)
# - agent_core_testing.responses: Response factories and step runner fixtures
# - mcp_infra.testing.fixtures: MCP compositor fixtures (compositor, compositor_client, etc.)
from agent_core_testing.fixtures import *  # noqa: F403
from agent_core_testing.openai_mock import NoopOpenAIClient
from agent_core_testing.responses import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture
def text_content():
    """Helper to create TextContent blocks for tests."""
    return lambda text: TextContent(text=text)


@pytest.fixture
async def noop_agent(mcp_tool_provider, recording_handler):
    """Agent with NoopOpenAIClient for testing message processing without sampling."""
    return await Agent.create(
        tool_provider=mcp_tool_provider,
        client=NoopOpenAIClient(),
        handlers=[recording_handler],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
