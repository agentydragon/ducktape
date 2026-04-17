"""Pytest config for grocy_mcp eval tests."""

from __future__ import annotations

import pytest

from x.grocy_mcp.grocy_fixtures import _preload_grocy, grocy_base_url, grocy_container  # noqa: F401


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
