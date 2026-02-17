"""OpenTelemetry tracing for claude_hooks.

Writes spans to a per-session JSON Lines file for post-hoc analysis.
Uses ConsoleSpanExporter with a file handle and compact JSON formatting.
"""

from __future__ import annotations

import logging
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

logger = logging.getLogger(__name__)


def _format_span(span: ReadableSpan) -> str:
    """Format a span as compact single-line JSON."""
    json_str: str = span.to_json(indent=None)
    return json_str + "\n"


def init_tracing(session_id: str, session_dir: Path) -> tuple[trace.Tracer, Path]:
    """Initialize OTel tracing with a per-session file exporter.

    Returns (tracer, trace_file_path).
    """
    trace_file = session_dir / "traces.jsonl"

    resource = Resource.create({"service.name": "claude-hooks", "session.id": session_id})
    provider = TracerProvider(resource=resource)
    exporter = ConsoleSpanExporter(out=trace_file.open("a"), formatter=_format_span)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    logger.info("Tracing initialized: %s", trace_file)
    return trace.get_tracer("claude-hooks"), trace_file


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
