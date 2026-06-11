"""Pytest configuration for dbus_fast_example tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
