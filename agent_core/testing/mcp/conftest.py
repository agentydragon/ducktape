"""Pytest configuration for agent_core.testing.mcp tests."""

from __future__ import annotations

import pytest

from agent_core.testing.responses import *  # noqa: F403

from agent_core.testing.mcp.responses import *  # noqa: F403  # isort:skip  # must shadow base responses_factory


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
