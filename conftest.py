"""Root pytest configuration for all packages.

Registers common markers and provides shared test infrastructure.
Per-package conftest.py files can extend this with package-specific fixtures.
"""

from __future__ import annotations

import faulthandler
import io
import os
import sys

import pytest

# Imported so the pytest-asyncio plugin rides //:conftest into every test's
# runfiles (pytest discovers installed plugins via entry points); the
# asyncio_mode setting below only takes effect when the plugin is present.
import pytest_asyncio


def pytest_configure(config: pytest.Config) -> None:
    """Register all common test markers."""
    # A test Bazel kills at its timeout leaves a log with nothing in it: stdout is block-buffered into a
    # file, and the kill gives Python no chance to report. Line-buffer stdout so the log shows how far it
    # got, and dump every thread's stack shortly before the deadline so it shows where it stalled.
    assert isinstance(sys.stdout, io.TextIOWrapper), sys.stdout
    sys.stdout.reconfigure(line_buffering=True)
    if timeout := os.getenv("TEST_TIMEOUT"):
        # A duplicate of the real stderr: fd capture points fd 2 at a temporary file while a test runs.
        faulthandler.dump_traceback_later(int(timeout) * 0.9, file=os.fdopen(os.dup(2), "w"))
    # Enable pytest-asyncio auto mode for all tests
    config.option.asyncio_mode = "auto"

    # External requirements - LLM APIs
    config.addinivalue_line("markers", "live_openai_api: tests requiring OPENAI_API_KEY")
    config.addinivalue_line(
        "markers", "httpx2(assert_all_called=False, assert_all_mocked=False, base_url=...): configure pytest-httpx2"
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail explicitly-run live tests without their required API key."""
    # fail() not skip(): pytest.skip surfaces as success at the Bazel level, hiding
    # the missing key. fail() ensures explicitly-run live targets fail loudly.
    # Wildcard runs (bazel test //...) exclude live targets via --test_tag_filters
    # in CI and Claude Code web bazelrc, so this only fires on explicit invocations.
    if item.get_closest_marker("live_openai_api") is not None and not os.getenv("OPENAI_API_KEY"):
        pytest.fail("OPENAI_API_KEY not set — cannot run live OpenAI test")
