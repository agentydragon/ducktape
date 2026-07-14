"""Root pytest configuration for all packages.

Registers common markers and provides shared test infrastructure.
Per-package conftest.py files can extend this with package-specific fixtures.
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register all common test markers."""
    # Enable pytest-asyncio auto mode for all tests
    config.option.asyncio_mode = "auto"

    # External requirements - LLM APIs
    config.addinivalue_line("markers", "live_openai_api: tests requiring OPENAI_API_KEY")


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail explicitly-run live tests without their required API key."""
    # fail() not skip(): pytest.skip surfaces as success at the Bazel level, hiding
    # the missing key. fail() ensures explicitly-run live targets fail loudly.
    # Wildcard runs (bazel test //...) exclude live targets via --test_tag_filters
    # in CI and Claude Code web bazelrc, so this only fires on explicit invocations.
    if item.get_closest_marker("live_openai_api") is not None and not os.getenv("OPENAI_API_KEY"):
        pytest.fail("OPENAI_API_KEY not set — cannot run live OpenAI test")
