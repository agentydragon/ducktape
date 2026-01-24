"""Pytest configuration for mcp_infra tests."""

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core_testing.fixtures import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
