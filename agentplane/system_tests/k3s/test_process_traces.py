"""OTLP fixture receiver accepts the protocols used by Agentplane processes."""

import json
from pathlib import Path
from urllib.request import Request, urlopen

import grpc
import pytest_bazel
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from agentplane.system_tests.k3s.process_traces import ProcessTraces


def test_process_spans_are_exported_with_identity_and_parentage(tmp_path: Path) -> None:
    request = trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(
                    attributes=[KeyValue(key="service.name", value=AnyValue(string_value="agentplane-runner"))]
                ),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name="runner"),
                        spans=[
                            Span(
                                trace_id=bytes.fromhex("01" * 16),
                                span_id=bytes.fromhex("02" * 8),
                                parent_span_id=bytes.fromhex("03" * 8),
                                name="agentplane.runner.session.open",
                                start_time_unix_nano=100,
                                end_time_unix_nano=200,
                                attributes=[
                                    KeyValue(key="session.id", value=AnyValue(string_value="s1")),
                                    KeyValue(key="agentplane.codex.generation", value=AnyValue(int_value=3)),
                                    KeyValue(key="authorization", value=AnyValue(string_value="secret")),
                                ],
                            )
                        ],
                    )
                ],
            )
        ]
    )
    path = tmp_path / "spans.jsonl"
    with ProcessTraces("127.0.0.1", path) as receiver:
        with grpc.insecure_channel(f"127.0.0.1:{receiver.grpc_port}") as channel:
            trace_service_pb2_grpc.TraceServiceStub(channel).Export(request)
        with urlopen(
            Request(
                f"http://127.0.0.1:{receiver.http_port}/v1/traces",
                data=request.SerializeToString(),
                headers={"Content-Type": "application/x-protobuf"},
            )
        ) as response:
            assert response.status == 200
        assert receiver.count == 2

    spans = [json.loads(line) for line in path.read_text().splitlines()]
    assert {span["trace_id"] for span in spans} == {"01" * 16}
    assert {span["parent_span_id"] for span in spans} == {"03" * 8}
    assert {span["service"] for span in spans} == {"agentplane-runner"}
    assert {span["attributes"]["session.id"] for span in spans} == {"s1"}
    assert {span["attributes"]["agentplane.codex.generation"] for span in spans} == {3}
    assert all("authorization" not in span["attributes"] for span in spans)


if __name__ == "__main__":
    pytest_bazel.main()
