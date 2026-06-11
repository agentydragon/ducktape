"""Shared pytest config for authentik_auth tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
