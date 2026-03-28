"""Unit tests for DeferredOtlpExporter."""

from unittest.mock import MagicMock, patch

import pytest_bazel
import requests
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult

from devinfra.claude.hook_config import OtelConfig
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter


def _make_session() -> requests.Session:
    return requests.Session()


def _make_config(endpoint: str = "https://otlp.example.com/v1/traces") -> OtelConfig:
    return OtelConfig(endpoint=endpoint)


def _make_span() -> MagicMock:
    return MagicMock(spec=ReadableSpan)


def test_configure_does_not_crash_on_export_failure() -> None:
    """configure() logs a warning and continues if the initial span flush fails."""
    exporter = DeferredOtlpExporter()
    # Buffer a span before configuring
    span = _make_span()
    exporter.export([span])
    assert len(exporter._buffer) == 1

    failing_session = MagicMock(spec=requests.Session)

    with patch("devinfra.claude.hook_daemon.tracing.OTLPSpanExporter") as mock_cls:
        mock_inner = MagicMock()
        mock_inner.export.side_effect = ConnectionError("unreachable")
        mock_cls.return_value = mock_inner

        # Must not raise even though export fails
        exporter.configure(_make_config(), session=failing_session)

    # Inner is set — configure completed despite flush failure
    assert exporter._inner is mock_inner
    # Buffer was drained
    assert exporter._buffer == []


def test_configure_idempotent() -> None:
    """Second configure() call with a different session is a no-op (first call wins)."""
    exporter = DeferredOtlpExporter()

    with patch("devinfra.claude.hook_daemon.tracing.OTLPSpanExporter") as mock_cls:
        mock_inner_1 = MagicMock()
        mock_inner_2 = MagicMock()
        mock_cls.side_effect = [mock_inner_1, mock_inner_2]

        exporter.configure(_make_config(), session=_make_session())
        exporter.configure(_make_config(), session=_make_session())

    # First call wins
    assert exporter._inner is mock_inner_1


def test_configure_no_endpoint_is_noop() -> None:
    """configure() with no endpoint leaves exporter unconfigured."""
    exporter = DeferredOtlpExporter()
    exporter.configure(OtelConfig(endpoint=None), session=_make_session())
    assert exporter._inner is None


def test_export_buffers_before_configure() -> None:
    """Spans exported before configure() are buffered and flushed on configure()."""
    exporter = DeferredOtlpExporter()
    span = _make_span()
    result = exporter.export([span])
    assert result == SpanExportResult.SUCCESS
    assert len(exporter._buffer) == 1

    with patch("devinfra.claude.hook_daemon.tracing.OTLPSpanExporter") as mock_cls:
        mock_inner = MagicMock()
        mock_inner.export.return_value = SpanExportResult.SUCCESS
        mock_cls.return_value = mock_inner

        exporter.configure(_make_config(), session=_make_session())

    mock_inner.export.assert_called_once_with([span])
    assert exporter._buffer == []


if __name__ == "__main__":
    pytest_bazel.main()
