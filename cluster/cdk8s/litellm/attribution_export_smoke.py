"""Run inside the pinned LiteLLM image with its own OpenTelemetry dependencies."""

import asyncio
from datetime import datetime

from litellm.integrations.otel.logger import OpenTelemetryV2
from litellm.integrations.otel.model.config import OpenTelemetryV2Config
from litellm.integrations.otel.plumbing.context import set_request_root_span
from litellm.integrations.otel.plumbing.providers import LiteLLMBaggageSpanProcessor
from litellm.litellm_core_utils.litellm_logging import create_dummy_standard_logging_payload
from opentelemetry import context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import DefaultGetter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

CLAIMS = {
    "agentplane.namespace": "agentplane-test",
    "agentplane.session_id": "00000000-0000-4000-8000-000000000055",
    "agentplane.service_account": "agentplane-test-runtime",
    "agentplane.pod_name": "agentplane-test-7f9c8",
    "agentplane.pod_uid": "00000000-0000-4000-8000-000000000001",
}
PREFIX = "litellm.metadata.spend_logs_metadata."
WIRE_BAGGAGE = ",".join(f"{PREFIX}{key}={value}" for key, value in CLAIMS.items())


def main() -> None:
    config = OpenTelemetryV2Config(
        mapper_names=["langfuse"],
        enable_events=False,
        enable_metrics=False,
        baggage_metadata_keys=[f"spend_logs_metadata.{key}" for key in CLAIMS],
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(LiteLLMBaggageSpanProcessor(allowed_keys=tuple(config.baggage_promoted_keys)))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    carrier = {"traceparent": "00-00000000000000000000000000000001-0000000000000002-01", "baggage": WIRE_BAGGAGE}
    propagator = CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    token = context.attach(propagator.extract(carrier, getter=DefaultGetter()))
    try:
        with provider.get_tracer("test").start_as_current_span("incoming HTTP request") as root:
            set_request_root_span(root)
            logger = OpenTelemetryV2(config=config, tracer_provider=provider)
            now = datetime.now()
            kwargs = {"litellm_call_id": "test-call", "model": "mock-model", "api_call_start_time": now}
            logger.log_pre_api_call("mock-model", [], kwargs)
            assert logger._open_llm_calls["test-call"].span is not None, "normal pre-call path was not used"
            payload = create_dummy_standard_logging_payload()
            payload["id"] = "test-call"
            payload["litellm_call_id"] = "test-call"
            kwargs["standard_logging_object"] = payload
            asyncio.run(logger.async_log_success_event(kwargs, {}, now, datetime.now()))
            assert "test-call" not in logger._open_llm_calls, "the normal pre-call span was not finished"
            root_id = root.get_span_context().span_id
    finally:
        context.detach(token)
        provider.shutdown()
    llm_spans = [span for span in exporter.get_finished_spans() if span.name != "incoming HTTP request"]
    assert len(llm_spans) == 1, llm_spans
    span = llm_spans[0]
    assert span.parent.span_id == root_id
    for key, value in CLAIMS.items():
        assert span.attributes[PREFIX + key] == value, (key, span.attributes)


if __name__ == "__main__":
    main()
