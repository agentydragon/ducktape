"""The fixture's services' own spans, received in the test process and written beside the test's.

Deployed services export to Alloy, which the k3s fixture does not run. `route_service_traces`
points every rendered OTLP exporter at a `TraceReceiver` on the node address instead, over
OTLP/HTTP, so the app, Runner Gateway, controllers and runners land in `service-spans.jsonl` in the
same JSON shape as the test's own spans in `vm-phases.jsonl`. The test's HTTP and gRPC clients
propagate trace context, so a service span joins the trace of the test call that caused it.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

_PROTOCOL_VARIABLES = ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")


def route_service_traces(value: Any, endpoint: str) -> None:
    """Point every container's OTLP exporter in rendered `value` at `endpoint`, in place."""
    if isinstance(value, dict):
        env = value.get("env")
        for variable in env if isinstance(env, list) else []:
            name = variable.get("name") if isinstance(variable, dict) else None
            if name == "OTEL_EXPORTER_OTLP_ENDPOINT":
                variable["value"] = endpoint
            elif name == "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT":
                variable["value"] = endpoint + "/v1/traces"
            elif name in _PROTOCOL_VARIABLES:
                variable["value"] = "http/protobuf"
        for child in value.values():
            route_service_traces(child, endpoint)
    elif isinstance(value, list):
        for child in value:
            route_service_traces(child, endpoint)


def _attributes(pairs: Any) -> dict[str, Any]:
    return {pair.key: next(iter(MessageToDict(pair.value).values()), None) for pair in pairs}


def _time(nanoseconds: int) -> str:
    return datetime.fromtimestamp(nanoseconds / 1e9, UTC).isoformat().replace("+00:00", "Z")


def _span(span: Span, resource: list[KeyValue]) -> dict[str, Any]:
    return {
        "name": span.name,
        "context": {"trace_id": "0x" + span.trace_id.hex(), "span_id": "0x" + span.span_id.hex()},
        "kind": Span.SpanKind.Name(span.kind),
        "parent_id": "0x" + span.parent_span_id.hex() if span.parent_span_id else None,
        "start_time": _time(span.start_time_unix_nano),
        "end_time": _time(span.end_time_unix_nano),
        "status": {"status_code": Status.StatusCode.Name(span.status.code), "description": span.status.message},
        "attributes": _attributes(span.attributes),
        "events": [
            {"name": event.name, "timestamp": _time(event.time_unix_nano), "attributes": _attributes(event.attributes)}
            for event in span.events
        ],
        "resource": {"attributes": _attributes(resource)},
    }


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(_Server, self.server)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        request = ExportTraceServiceRequest.FromString(body)
        lines = [
            json.dumps(_span(span, list(resource_spans.resource.attributes)))
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]
        with server.lock, server.path.open("a") as out:
            out.writelines(line + "\n" for line in lines)
        reply = ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str) -> None:
        super().__init__((host, 0), _Handler)
        self.lock = threading.Lock()
        self.path = undeclared_outputs_dir() / "service-spans.jsonl"


@contextmanager
def trace_receiver(host: str) -> Iterator[str]:
    """An OTLP/HTTP trace receiver on `host` for the stack's lifetime; yields its endpoint."""
    server = _Server(host)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
