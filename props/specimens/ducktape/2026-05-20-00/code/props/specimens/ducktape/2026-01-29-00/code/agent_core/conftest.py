"""Pytest configuration for agent_core tests."""

from __future__ import annotations

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
# - agent_core_testing.fixtures: Core agent fixtures (recording_handler, make_test_agent, etc.)
# - agent_core_testing.responses: Response factories and step runner fixtures
# - mcp_infra.testing.fixtures: MCP compositor fixtures (compositor, compositor_client, etc.)
from agent_core_testing.fixtures import *  # noqa: F403
from agent_core_testing.responses import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
