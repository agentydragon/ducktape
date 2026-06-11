"""OpenTelemetry tracing for the claude hook daemon.

Two exporters:
- Local JSONL file (always, for post-hoc analysis)
- Remote OTLP/HTTP (when OtelConfig with endpoint+token is provided at startup)
"""

import logging
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from devinfra.claude.hook_daemon.config import OtelConfig
from util.otel import JsonlSpanExporter

logger = logging.getLogger(__name__)

# Default flush timeout. 500ms is enough for a healthy local/nearby endpoint.
DEFAULT_FLUSH_TIMEOUT_MS = 500


def _build_otlp_headers(config: OtelConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.bearer_token:
        # Authentik proxy expects "Bearer <token>".
        value = config.bearer_token if " " in config.bearer_token else f"Bearer {config.bearer_token}"
        headers["Authorization"] = value
    return headers


def init_daemon_tracing(trace_dir: Path, otel_config: OtelConfig | None = None) -> None:
    """Initialize OTel tracing with a local file exporter and optional OTLP.

    Called once at daemon startup. Sets the global TracerProvider.
    Callers get tracers via trace.get_tracer(__name__).
    """
    trace_file = trace_dir / "traces.jsonl"
    resource = Resource.create({"service.name": "claude-hooks"})
    provider = TracerProvider(resource=resource)

    file_exporter = JsonlSpanExporter(trace_file)
    provider.add_span_processor(BatchSpanProcessor(file_exporter))
    logger.info("Tracing: local file → %s", trace_file)

    if otel_config and otel_config.endpoint:
        otlp_exporter = OTLPSpanExporter(endpoint=otel_config.endpoint, headers=_build_otlp_headers(otel_config))
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("Tracing: OTLP → %s", otel_config.endpoint)

    # Force-set provider even if one already exists (e.g., from OTEL_* auto-config).
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace.set_tracer_provider(provider)


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
