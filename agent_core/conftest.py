"""Pytest configuration for agent_core tests."""

from __future__ import annotations

import pytest

# Import fixtures from testing modules
# - agent_core.testing.fixtures: Core agent fixtures (recording_handler, make_test_agent, etc.)
# - agent_core.testing.responses: Response factories and step runner fixtures
# - agent_core.testing.mcp.*: MCP-dependent testing utilities
# - mcp_infra.testing.fixtures: MCP compositor fixtures (compositor, compositor_client, etc.)
from agent_core.testing.fixtures import *  # noqa: F403
from agent_core.testing.mcp.fixtures import *  # noqa: F403
from agent_core.testing.mcp.responses import *  # noqa: F403
from agent_core.testing.responses import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403
from openai_utils.testing.fixtures import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
