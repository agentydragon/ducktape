"""Shared pytest config for grocy_mcp tests."""

from __future__ import annotations

import pytest

from x.grocy_mcp.grocy_fixtures import grocy_base_url, grocy_container


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
