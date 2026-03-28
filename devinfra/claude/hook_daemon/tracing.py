"""OpenTelemetry tracing for the claude hook daemon.

Initialized once at daemon startup via init_daemon_tracing(), which returns a
DeferredOtlpExporter. The exporter buffers spans in memory until configure() is
called with OTLP credentials (fetched from k8s secrets during session start).

Two exporters:
- Local JSONL file (always, for post-hoc analysis)
- Remote OTLP/HTTP (via DeferredOtlpExporter, active after k8s secrets are fetched)
"""

import logging
import threading
from collections.abc import Sequence
from pathlib import Path

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import ProxyTracerProvider

from devinfra.claude.hook_config import OtelConfig

logger = logging.getLogger(__name__)

# Default flush timeout. 500ms is enough for a healthy local/nearby endpoint.
DEFAULT_FLUSH_TIMEOUT_MS = 500


def _format_span(span: ReadableSpan) -> str:
    """Format a span as compact single-line JSON."""
    json_str: str = span.to_json(indent=None)
    return json_str + "\n"


def _build_otlp_headers(config: OtelConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.bearer_token:
        # Authentik proxy expects "Bearer <token>". The auth_token value is
        # the raw Authentik service account key from the k8s secret.
        value = config.bearer_token if " " in config.bearer_token else f"Bearer {config.bearer_token}"
        headers["Authorization"] = value
    return headers


class DeferredOtlpExporter(SpanExporter):
    """OTLP span exporter that buffers spans until configure() is called.

    Added to the TracerProvider at daemon startup. Spans accumulate in memory
    until the first session provides OTLP credentials (from k8s secrets).
    configure() flushes the buffer to OTLP and forwards all future spans directly.

    Idempotent: configure() is a no-op after the first successful call.
    Thread-safe: export() and configure() may be called concurrently.
    """

    def __init__(self) -> None:
        self._inner: OTLPSpanExporter | None = None
        self._buffer: list[ReadableSpan] = []
        self._lock = threading.Lock()

    def configure(self, config: OtelConfig, session: requests.Session) -> None:
        """Configure the OTLP endpoint. Flushes buffered spans. Idempotent."""
        if not config.endpoint:
            return
        inner = OTLPSpanExporter(endpoint=config.endpoint, headers=_build_otlp_headers(config), session=session)
        with self._lock:
            if self._inner is not None:
                logger.debug("OTLP exporter already configured, skipping")
                return
            self._inner = inner
            buffered, self._buffer = self._buffer, []
        if buffered:
            logger.info("OTLP: flushing %d buffered spans", len(buffered))
            try:
                inner.export(buffered)
            except Exception as e:
                logger.warning("OTLP: failed to flush %d buffered spans (non-fatal): %s", len(buffered), e)
        logger.info("Tracing: OTLP → %s", config.endpoint)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            if self._inner is None:
                self._buffer.extend(spans)
                return SpanExportResult.SUCCESS
            inner = self._inner
        return inner.export(spans)

    def shutdown(self) -> None:
        with self._lock:
            inner = self._inner
        if inner:
            inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        with self._lock:
            inner = self._inner
        return inner.force_flush(timeout_millis) if inner else True


def init_daemon_tracing(trace_dir: Path) -> DeferredOtlpExporter:
    """Initialize OTel tracing with a local file exporter and deferred OTLP.

    Called once at daemon startup. Sets the global TracerProvider. Returns a
    DeferredOtlpExporter that buffers spans until configure() is called with
    OTLP credentials.

    Callers get tracers via trace.get_tracer(__name__).
    """
    # Warn if provider is already set — indicates OTEL_* env var auto-configuration
    # or a library calling set_tracer_provider() before us. This helps diagnose the
    # "Overriding of current TracerProvider is not allowed" warning.
    existing = trace.get_tracer_provider()
    if not isinstance(existing, ProxyTracerProvider):
        logger.warning(
            "TracerProvider already set before init_daemon_tracing: %s — "
            "possible OTEL_* env var auto-configuration; set_tracer_provider will warn",
            type(existing).__name__,
        )

    trace_file = trace_dir / "traces.jsonl"
    resource = Resource.create({"service.name": "claude-hooks"})
    provider = TracerProvider(resource=resource)

    file_exporter = ConsoleSpanExporter(out=trace_file.open("a"), formatter=_format_span)
    provider.add_span_processor(SimpleSpanProcessor(file_exporter))
    logger.info("Tracing: local file → %s", trace_file)

    otlp = DeferredOtlpExporter()
    provider.add_span_processor(BatchSpanProcessor(otlp))

    trace.set_tracer_provider(provider)
    return otlp


def flush(timeout_ms: int = DEFAULT_FLUSH_TIMEOUT_MS) -> None:
    """Flush buffered spans. Warns and returns if the endpoint is slow/down."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return
    if not provider.force_flush(timeout_millis=timeout_ms):
        logger.warning("OTEL: flush timed out after %dms — endpoint may be unreachable. Spans may be lost.", timeout_ms)


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
