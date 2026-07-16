"""Pytest configuration for wt/server tests."""

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from wt.testing.conftest import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
