"""Conftest for agent_server/mcp tests."""

import pytest

# Import fixtures
from mcp_infra.testing.fixtures import compositor, make_typed_mcp  # noqa: F401


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
