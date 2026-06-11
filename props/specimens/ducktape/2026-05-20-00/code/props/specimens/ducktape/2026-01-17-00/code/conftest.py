"""Root pytest configuration for all packages.

Registers common markers and provides shared test infrastructure.
Per-package conftest.py files can extend this with package-specific fixtures.
"""

from __future__ import annotations

import os
import platform
from contextlib import suppress

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register all common test markers."""
    # Test categories
    config.addinivalue_line("markers", "unit: fast unit tests")
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "slow: marks tests as slow")
    config.addinivalue_line("markers", "e2e: end-to-end UI tests using playwright")

    # External requirements - LLM APIs
    config.addinivalue_line("markers", "live_openai_api: tests requiring OPENAI_API_KEY")
    config.addinivalue_line("markers", "live_anthropic_api: tests requiring ANTHROPIC_API_KEY")
    config.addinivalue_line("markers", "real_github: tests requiring network access to GitHub")
    config.addinivalue_line("markers", "requires_docker: tests requiring Docker daemon")
    config.addinivalue_line("markers", "requires_postgres: tests requiring PostgreSQL database")
    config.addinivalue_line("markers", "requires_sandbox_exec: tests requiring macOS sandbox-exec")
    config.addinivalue_line("markers", "requires_production_specimens: tests that sync production specimens")

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

    # Docker availability check
    if item.get_closest_marker("requires_docker") is not None:
        try:
            import docker  # noqa: PLC0415 - optional dependency, lazy import

            client = docker.from_env()
            client.ping()
            with suppress(Exception):
                client.close()
        except Exception as exc:
            pytest.skip(f"Docker not available: {exc}")

    # PostgreSQL availability check
    if item.get_closest_marker("requires_postgres") is not None:
        pg_host = os.getenv("PGHOST")
        if not pg_host:
            pytest.skip("PGHOST not set - PostgreSQL not configured")

    # LLM API key requirements
    if item.get_closest_marker("live_openai_api") is not None and not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")

    if item.get_closest_marker("live_anthropic_api") is not None and not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Automatically add markers based on other markers."""
    for item in items:
        # sandbox-exec tests are implicitly macOS-only
        if item.get_closest_marker("requires_sandbox_exec") is not None:
            item.add_marker(pytest.mark.macos)
