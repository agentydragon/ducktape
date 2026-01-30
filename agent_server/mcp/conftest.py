"""Conftest for agent_server/mcp tests."""

import pytest

# Import fixtures
from mcp_infra.testing.fixtures import (  # noqa: F401
    compositor,
    make_buffered_client,
    make_typed_mcp,
    stdio_notifier_spec,
)


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
