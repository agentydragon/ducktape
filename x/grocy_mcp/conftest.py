"""Shared pytest config for grocy_mcp tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from opentelemetry import trace

from util.testing.otel_tracing import configure_tracing
from x.grocy_mcp.grocy_fixtures import grocy_base_url, grocy_container


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
    configure_tracing(config)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None]:
    """Create a root span for each test for hierarchical traces."""
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(f"test: {item.nodeid}"):
        yield
