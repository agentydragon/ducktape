"""OpenTelemetry tracing for test profiling.

Configures a TracerProvider with an in-memory exporter, allowing custom spans
and exporting to JSON in TEST_UNDECLARED_OUTPUTS_DIR.

Usage:
    # In conftest.py
    from props.testing.otel_tracing import tracing

    def pytest_configure(config):
        tracing.configure()

    def pytest_sessionfinish(session, exitstatus):
        tracing.export_to_file()
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)


class TracingConfig:
    """Holds OTel tracing configuration for test profiling."""

    def __init__(self) -> None:
        self.exporter: InMemorySpanExporter | None = None
        self.provider: TracerProvider | None = None

    def configure(self) -> None:
        """Configure OTel with in-memory exporter. Call once at session start."""
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        trace.set_tracer_provider(self.provider)
        logger.debug("OTel tracing configured with in-memory exporter")

    def export_to_file(self) -> Path | None:
        """Export collected spans to TEST_UNDECLARED_OUTPUTS_DIR/traces.json.

        Returns the path written, or None if no output dir or no exporter configured.
        """
        if self.exporter is None:
            logger.debug("No OTel exporter configured, skipping trace export")
            return None

        spans = self.exporter.get_finished_spans()
        if not spans:
            logger.debug("No spans collected, skipping trace export")
            return None

        traces = [_span_to_dict(s) for s in spans]

        dest = undeclared_outputs_dir() / "traces.json"
        dest.write_text(json.dumps(traces, indent=2))
        logger.info("Exported %d spans to %s", len(traces), dest)
        return dest


def _span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    """Convert a ReadableSpan to a JSON-serializable dict."""
    parent_span_id = None
    if span.parent is not None:
        parent_span_id = format(span.parent.span_id, "016x")

    return {
        "name": span.name,
        "start_time_ns": span.start_time,
        "end_time_ns": span.end_time,
        "duration_ms": (span.end_time - span.start_time) / 1_000_000 if span.end_time and span.start_time else None,
        "parent_span_id": parent_span_id,
        "span_id": format(span.context.span_id, "016x"),
        "trace_id": format(span.context.trace_id, "032x"),
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes) if span.attributes else {},
    }


# Module-level instance
tracing = TracingConfig()
