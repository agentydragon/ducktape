"""OpenTelemetry tracing for test profiling.

Configures a TracerProvider that writes spans to JSONL in
TEST_UNDECLARED_OUTPUTS_DIR immediately as each span ends. Spans are
flushed to disk on completion via SimpleSpanProcessor, so traces survive
even if the test is killed by Bazel timeout (SIGKILL).

Usage in conftest.py:

    from util.testing.otel_tracing import configure_tracing

    def pytest_configure(config):
        configure_tracing(config)
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from util.otel import JsonlSpanExporter
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)


def configure_tracing(config=None, filename: str = "otel_spans.jsonl") -> None:
    """Set up OTel with streaming JSONL exporter. Call from pytest_configure."""
    dest = undeclared_outputs_dir() / filename
    exporter = JsonlSpanExporter(dest)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.debug("OTel tracing configured, streaming to %s", dest)
