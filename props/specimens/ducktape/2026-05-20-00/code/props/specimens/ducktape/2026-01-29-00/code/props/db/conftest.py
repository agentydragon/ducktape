"""Pytest configuration for props/core/db tests.

Fixtures are provided via deps chain (//props/core:conftest -> //props/testing).
This conftest only configures pytest-asyncio auto mode.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
