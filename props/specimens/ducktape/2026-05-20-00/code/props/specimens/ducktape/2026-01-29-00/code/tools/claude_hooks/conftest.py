"""Pytest configuration for claude_hooks tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
