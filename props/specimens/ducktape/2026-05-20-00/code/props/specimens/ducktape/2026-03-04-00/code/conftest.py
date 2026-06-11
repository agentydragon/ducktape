"""Root pytest configuration for all packages.

Registers common markers and provides shared test infrastructure.
Per-package conftest.py files can extend this with package-specific fixtures.
"""

from __future__ import annotations

import os
import platform

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register all common test markers."""
    # Enable pytest-asyncio auto mode for all tests
    config.option.asyncio_mode = "auto"

    # Test categories
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "slow: marks tests as slow")
    config.addinivalue_line("markers", "e2e: end-to-end UI tests using playwright")

    # External requirements - LLM APIs
    config.addinivalue_line("markers", "live_openai_api: tests requiring OPENAI_API_KEY")
    config.addinivalue_line("markers", "real_github: tests requiring network access to GitHub")
    config.addinivalue_line("markers", "requires_sandbox_exec: tests requiring macOS sandbox-exec")

    # Platform markers
    config.addinivalue_line("markers", "macos: macOS-only tests")
    config.addinivalue_line("markers", "shell: shell integration tests")
    config.addinivalue_line("markers", "asyncio: tests that use pytest-asyncio")


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip tests based on environment and markers."""
    # macOS-only tests
    if item.get_closest_marker("macos") is not None and platform.system() != "Darwin":
        pytest.skip("macOS-only test")

    # sandbox-exec requires macOS
    if item.get_closest_marker("requires_sandbox_exec") is not None and platform.system() != "Darwin":
        pytest.skip("sandbox-exec requires macOS")

    # LLM API key requirements
    # fail() not skip(): pytest.skip surfaces as success at the Bazel level, hiding
    # the missing key. fail() ensures explicitly-run live targets fail loudly.
    # Wildcard runs (bazel test //...) exclude live targets via --test_tag_filters
    # in CI and Claude Code web bazelrc, so this only fires on explicit invocations.
    if item.get_closest_marker("live_openai_api") is not None and not os.getenv("OPENAI_API_KEY"):
        pytest.fail("OPENAI_API_KEY not set — cannot run live OpenAI test")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Automatically add markers based on other markers."""
    for item in items:
        # sandbox-exec tests are implicitly macOS-only
        if item.get_closest_marker("requires_sandbox_exec") is not None:
            item.add_marker(pytest.mark.macos)
