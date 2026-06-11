"""Pytest configuration for openai_utils tests."""

from __future__ import annotations

import os

import openai
import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture
def require_openai_api_key():
    """Skip test if OPENAI_API_KEY is not set."""
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")


@pytest.fixture
def live_openai_model() -> str:
    """Return the model to use for live tests."""
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


@pytest.fixture
def live_async_openai() -> openai.AsyncOpenAI:
    """Return an AsyncOpenAI client for live tests."""
    return openai.AsyncOpenAI()
