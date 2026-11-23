"""OpenTelemetry tracing for test profiling.

Configures a TracerProvider that writes spans to JSONL in
TEST_UNDECLARED_OUTPUTS_DIR immediately as each span ends. Spans are
flushed to disk on completion via SimpleSpanProcessor, so traces survive
even if the test is killed by Bazel timeout (SIGKILL).

To include fixture setup and teardown in a root span per test, expose the hook in conftest.py:

    from testing.otel_tracing import configure_tracing, pytest_runtest_protocol

    def pytest_configure():
        configure_tracing()
"""

from __future__ import annotations

import logging
from collections.abc import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.otel import JsonlSpanExporter

logger = logging.getLogger(__name__)


def configure_tracing(filename: str = "otel_spans.jsonl") -> None:
    """Set up OTel with streaming JSONL exporter. Call from pytest_configure."""
    dest = undeclared_outputs_dir() / filename
    exporter = JsonlSpanExporter(dest)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.debug("OTel tracing configured, streaming to %s", dest)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None]:
    """Parent setup, call and teardown spans to the same pytest case."""
    with trace.get_tracer(__name__).start_as_current_span(f"test: {item.nodeid}"):
        yield
