"""pytest configuration for auth_proxy tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio to auto mode."""
    config.option.asyncio_mode = "auto"
    config._inicache["asyncio_default_fixture_loop_scope"] = "function"
