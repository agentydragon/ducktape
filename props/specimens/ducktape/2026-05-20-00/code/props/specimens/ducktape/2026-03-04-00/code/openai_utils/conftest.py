"""Pytest configuration for openai_utils tests."""

from __future__ import annotations

import pytest

from openai_utils.testing.fixtures import live_openai, live_openai_model

__all__ = ["live_openai", "live_openai_model"]


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
