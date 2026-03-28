"""Unit tests for DeferredOtlpExporter."""

import pytest
import pytest_bazel
import requests
import requests.adapters
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult

from devinfra.claude.hook_config import OtelConfig
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter

_ENDPOINT = "https://otlp.example.com/v1/traces"


class _FailingAdapter(requests.adapters.HTTPAdapter):
    def send(self, *args, **kwargs):
        raise requests.exceptions.ConnectionError("unreachable")


@pytest.fixture
def session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _FailingAdapter())
    return s


@pytest.fixture
def span() -> ReadableSpan:
    return ReadableSpan(name="test-span")


def test_configure_does_not_crash_on_export_failure(session: requests.Session, span: ReadableSpan) -> None:
    """configure() logs a warning and continues if the initial span flush fails."""
    exporter = DeferredOtlpExporter()
    exporter.export([span])

    exporter.configure(OtelConfig(endpoint=_ENDPOINT), session=session)

    assert exporter._inner is not None
    assert exporter._buffer == []


def test_configure_idempotent(session: requests.Session) -> None:
    """Second configure() call is a no-op (first caller wins)."""
    exporter = DeferredOtlpExporter()
    config = OtelConfig(endpoint=_ENDPOINT)
    exporter.configure(config, session=session)
    inner = exporter._inner
    exporter.configure(config, session=session)
    assert exporter._inner is inner


def test_configure_no_endpoint_is_noop(session: requests.Session) -> None:
    exporter = DeferredOtlpExporter()
    exporter.configure(OtelConfig(endpoint=None), session=session)
    assert exporter._inner is None


def test_export_buffers_before_configure(span: ReadableSpan) -> None:
    exporter = DeferredOtlpExporter()
    assert exporter.export([span]) == SpanExportResult.SUCCESS
    assert exporter._buffer == [span]


if __name__ == "__main__":
    pytest_bazel.main()
