"""Unit tests for tracing initialization."""

import pytest
import pytest_bazel
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from devinfra.claude.hook_daemon.config import OtelConfig
from devinfra.claude.hook_daemon.tracing import init_daemon_tracing


@pytest.fixture(autouse=True)
def _reset_tracer_provider():
    """Reset global TracerProvider so set_tracer_provider() works in each test."""
    trace._TRACER_PROVIDER_SET_ONCE._done = False


def test_init_no_otel_config(tmp_path) -> None:
    """Without OtelConfig, only the JSONL file exporter is added."""
    init_daemon_tracing(tmp_path)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # One processor: JSONL file exporter
    assert len(provider._active_span_processor._span_processors) == 1


def test_init_with_otel_config(tmp_path) -> None:
    """With OtelConfig, both JSONL and OTLP exporters are added."""
    config = OtelConfig(endpoint="https://otlp.example.com/v1/traces", bearer_token="test-token")
    init_daemon_tracing(tmp_path, otel_config=config)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # Two processors: JSONL + OTLP
    assert len(provider._active_span_processor._span_processors) == 2


def test_init_with_no_endpoint(tmp_path) -> None:
    """OtelConfig with no endpoint skips OTLP exporter."""
    config = OtelConfig(endpoint=None)
    init_daemon_tracing(tmp_path, otel_config=config)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert len(provider._active_span_processor._span_processors) == 1


if __name__ == "__main__":
    pytest_bazel.main()
