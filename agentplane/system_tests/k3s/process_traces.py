"""Fixture-owned OTLP receiver for service and runner spans in k3s tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, override

import grpc
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.proto.common.v1.common_pb2 import KeyValue

if TYPE_CHECKING:
    from opentelemetry.proto.trace.v1.trace_pb2 import Span

_SAFE_ATTRIBUTES = frozenset(
    {
        "service.name",
        "service.namespace",
        "service.instance.id",
        "process.pid",
        "deployment.environment.name",
        "sandbox.name",
        "sandbox.uid",
        "session.id",
        "thread.id",
        "harness",
        "rpc.service",
        "rpc.method",
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "rpc.grpc.status_code",
        "agentplane.session.opening.position",
        "agentplane.codex.generation",
        "agentplane.runner.quiesce.stage",
    }
)
_SAFE_EVENTS = frozenset(
    {
        "agentplane.runner.quiesce_stage",
        "agentplane.gateway.runner.quiesce_stage_observed",
        "agentplane.gateway.runner.quiesce_stage_published",
    }
)
_SAFE_QUIESCE_STAGES = frozenset({"none", "draining", "flushing", "quiesced"})
_QUIESCE_STAGE_ATTRIBUTE = "agentplane.runner.quiesce.stage"


def _attributes(items: Iterable[KeyValue]) -> dict[str, str | int | bool]:
    values: dict[str, str | int | bool] = {}
    for item in items:
        if item.key not in _SAFE_ATTRIBUTES:
            continue
        kind = item.value.WhichOneof("value")
        if kind == "string_value":
            values[item.key] = item.value.string_value
        elif kind == "int_value":
            values[item.key] = item.value.int_value
        elif kind == "bool_value":
            values[item.key] = item.value.bool_value
    return values


def _events(items: Iterable[Span.Event]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for item in items:
        if item.name not in _SAFE_EVENTS:
            continue
        stage = _attributes(item.attributes).get(_QUIESCE_STAGE_ATTRIBUTE)
        if stage not in _SAFE_QUIESCE_STAGES:
            continue
        events.append(
            {"name": item.name, "time_unix_nano": item.time_unix_nano, "attributes": {_QUIESCE_STAGE_ATTRIBUTE: stage}}
        )
    return events


class _Receiver(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a")
        self._lock = threading.Lock()
        self.count = 0

    @override
    def Export(
        self, request: trace_service_pb2.ExportTraceServiceRequest, context: grpc.ServicerContext
    ) -> trace_service_pb2.ExportTraceServiceResponse:
        self.record(request, source=context.peer())
        return trace_service_pb2.ExportTraceServiceResponse()

    def record(self, request: trace_service_pb2.ExportTraceServiceRequest, *, source: str) -> None:
        with self._lock:
            for resource_spans in request.resource_spans:
                resource = _attributes(resource_spans.resource.attributes)
                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        self._write_span(span, resource=resource, scope=scope_spans.scope.name, source=source)
            self._file.flush()

    def _write_span(self, span: Span, *, resource: dict[str, str | int | bool], scope: str, source: str) -> None:
        self._file.write(
            json.dumps(
                {
                    "trace_id": span.trace_id.hex(),
                    "span_id": span.span_id.hex(),
                    "parent_span_id": span.parent_span_id.hex(),
                    "name": span.name,
                    "start_time_unix_nano": span.start_time_unix_nano,
                    "end_time_unix_nano": span.end_time_unix_nano,
                    "service": resource.get("service.name"),
                    "resource": resource,
                    "attributes": _attributes(span.attributes),
                    "events": _events(span.events),
                    "scope": scope,
                    "source": source,
                    "status": span.status.code,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        self.count += 1

    def close(self) -> None:
        with self._lock:
            self._file.close()


class ProcessTraces:
    """Receives OTLP/gRPC and OTLP/HTTP on node-reachable ephemeral ports."""

    def __init__(self, host: str, path: Path) -> None:
        self._host = host
        self._path = path

    def __enter__(self) -> ProcessTraces:
        with ExitStack() as cleanup:
            receiver = _Receiver(self._path)
            cleanup.callback(receiver.close)
            executor = cleanup.enter_context(ThreadPoolExecutor(max_workers=4))
            server = grpc.server(executor)
            trace_service_pb2_grpc.add_TraceServiceServicer_to_server(receiver, server)
            grpc_port = server.add_insecure_port(f"{self._host}:0")
            if not grpc_port:
                raise RuntimeError(f"cannot bind OTLP/gRPC on {self._host}")
            server.start()
            cleanup.callback(lambda: server.stop(grace=5).wait())

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    if self.path != "/v1/traces":
                        self.send_error(404)
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        self.send_error(400)
                        return
                    if length <= 0 or length > 8 * 1024 * 1024:
                        self.send_error(413)
                        return
                    request = trace_service_pb2.ExportTraceServiceRequest()
                    try:
                        request.ParseFromString(self.rfile.read(length))
                    except DecodeError:
                        self.send_error(400)
                        return
                    receiver.record(request, source=self.client_address[0])
                    response = trace_service_pb2.ExportTraceServiceResponse().SerializeToString()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-protobuf")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

                def log_message(self, format: str, *args: object) -> None:
                    pass

            http = ThreadingHTTPServer((self._host, 0), Handler)
            cleanup.callback(http.server_close)
            thread = threading.Thread(target=http.serve_forever, name="otlp-http", daemon=True)
            thread.start()
            cleanup.callback(thread.join)
            cleanup.callback(http.shutdown)
            self.grpc_port = grpc_port
            self.http_port = http.server_port
            self._receiver = receiver
            self._cleanup = cleanup.pop_all()
            return self

    def __exit__(self, *exc: object) -> None:
        self._cleanup.close()

    @property
    def count(self) -> int:
        return self._receiver.count


def collector_objects(host: str, traces: ProcessTraces) -> list[dict[str, object]]:
    """Expose the host receiver at the production Alloy service name and ports."""
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "monitoring"}},
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "alloy", "namespace": "monitoring"},
            "spec": {
                "ports": [
                    {"name": "otlp-grpc", "port": 4317, "targetPort": traces.grpc_port},
                    {"name": "otlp-http", "port": 4318, "targetPort": traces.http_port},
                ]
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Endpoints",
            "metadata": {"name": "alloy", "namespace": "monitoring"},
            "subsets": [
                {
                    "addresses": [{"ip": host}],
                    "ports": [
                        {"name": "otlp-grpc", "port": traces.grpc_port},
                        {"name": "otlp-http", "port": traces.http_port},
                    ],
                }
            ],
        },
    ]
