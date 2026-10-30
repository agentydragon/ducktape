"""Root pytest configuration for all packages.

Registers common markers and provides shared test infrastructure.
Per-package conftest.py files can extend this with package-specific fixtures.
"""

from __future__ import annotations

import os
import time

import pytest

# Imported so the pytest-asyncio plugin rides //:conftest into every test's
# runfiles (pytest discovers installed plugins via entry points); the
# asyncio_mode setting below only takes effect when the plugin is present.
import pytest_asyncio

# Imported so the pytest-timeout plugin rides //:conftest into every test's runfiles, as pytest_asyncio does.
import pytest_timeout

_STARTED = time.monotonic()


def pytest_configure(config: pytest.Config) -> None:
    """Register all common test markers."""
    # Enable pytest-asyncio auto mode for all tests
    config.option.asyncio_mode = "auto"

    # External requirements - LLM APIs
    config.addinivalue_line(
        "markers", "live_openai_api(credential_env='OPENAI_API_KEY'): tests requiring a live provider key"
    )
    config.addinivalue_line(
        "markers", "httpx2(assert_all_called=False, assert_all_mocked=False, base_url=...): configure pytest-httpx2"
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> object:
    """Fail a test still running shortly before Bazel's timeout, instead of being killed with nothing reported.

    Bazel kills the whole process at `TEST_TIMEOUT`, which leaves an empty log. pytest-timeout instead fails
    the stalled test with every thread's stack, so pytest reports it and the rest of the run's output survives.
    """
    if (budget := os.getenv("TEST_TIMEOUT")) and item.get_closest_marker("timeout") is None:
        item.add_marker(pytest.mark.timeout(max(int(budget) * 0.9 - (time.monotonic() - _STARTED), 1)))
    return (yield)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail explicitly-run live tests without their required API key."""
    # fail() not skip(): pytest.skip surfaces as success at the Bazel level, hiding
    # the missing key. fail() ensures explicitly-run live targets fail loudly.
    # Wildcard runs (bazel test //...) exclude live targets via --test_tag_filters
    # in CI and Claude Code web bazelrc, so this only fires on explicit invocations.
    if marker := item.get_closest_marker("live_openai_api"):
        credential_env = marker.kwargs.get("credential_env", "OPENAI_API_KEY")
        if not os.getenv(credential_env):
            pytest.fail(f"{credential_env} not set — cannot run live provider test")
