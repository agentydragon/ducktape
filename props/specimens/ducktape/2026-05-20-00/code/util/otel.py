"""Shared OpenTelemetry utilities."""

from collections.abc import Sequence
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class JsonlSpanExporter(SpanExporter):
    """Appends each span as single-line JSON to a file, flushing immediately."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            self._file.write(span.to_json(indent=None) + "\n")
        self._file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._file.close()
