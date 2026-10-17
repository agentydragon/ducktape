"""Fixtures for compositor tests."""

from __future__ import annotations

import pytest
from fastmcp.client import Client

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core.testing.fixtures import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403
from x.agent_cli.compositor.compositor import Compositor


@pytest.fixture
async def compositor():
    """Fresh agent CLI compositor with automatic lifecycle management."""
    async with Compositor() as comp:
        yield comp


@pytest.fixture
async def compositor_client(compositor):
    """Client connected to the compositor."""
    async with Client(compositor) as client:
        yield client


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
