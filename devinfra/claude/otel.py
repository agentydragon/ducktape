"""OTLP trace exporter for claude → Grafana Alloy → Tempo.

Configured via OtelConfig (from .claude_hooks/config.yaml + env var overrides).
"""

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from devinfra.claude.hook_config import OtelConfig

logger = logging.getLogger(__name__)

# Default flush timeout. 500ms is enough for a healthy local/nearby endpoint.
# If the endpoint is down, we warn and move on rather than blocking for seconds.
DEFAULT_FLUSH_TIMEOUT_MS = 500


def init_from_config(config: OtelConfig) -> None:
    """Initialize OTLP tracing. No-op if endpoint is not set."""
    if not config.endpoint:
        return

    headers: dict[str, str] = {}
    if config.bearer_token:
        # Authentik proxy expects "Bearer <token>". The auth_token value is
        # the raw Authentik service account key from the k8s secret.
        value = config.bearer_token if " " in config.bearer_token else f"Bearer {config.bearer_token}"
        headers["Authorization"] = value

    exporter = OTLPSpanExporter(endpoint=config.endpoint, headers=headers)
    resource = Resource.create({"service.name": "claude-hooks"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("OTEL: traces → %s", config.endpoint)


def flush(timeout_ms: int = DEFAULT_FLUSH_TIMEOUT_MS) -> None:
    """Flush buffered spans. Warns and returns if the endpoint is slow/down."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return  # No SDK provider configured (ProxyTracerProvider or similar).
    if not provider.force_flush(timeout_millis=timeout_ms):
        logger.warning(
            "OTEL: flush timed out after %dms — endpoint may be unreachable. Spans from this invocation will be lost.",
            timeout_ms,
        )
