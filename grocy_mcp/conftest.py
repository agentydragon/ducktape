"""Shared pytest config for grocy_mcp tests."""

from __future__ import annotations

from grocy_mcp.grocy_fixtures import grocy_base_url, grocy_container
from testing.otel_tracing import configure_tracing, pytest_runtest_protocol


def pytest_configure() -> None:
    configure_tracing()
