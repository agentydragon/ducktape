import pytest_bazel
from google.protobuf import json_format

from devinfra.x.schema_codegen.protobuf import sample_pb2
from devinfra.x.schema_codegen.protobuf.python_producer import sample_proto_json


def test_generated_python_protojson_round_trip() -> None:
    decoded = json_format.Parse(sample_proto_json("python"), sample_pb2.Sample())

    assert decoded.sample_id == "sample-17"
    assert decoded.sequence == 9_007_199_254_740_993
    assert decoded.status == 37
    assert decoded.HasField("note")
    assert decoded.note == ""
    assert decoded.WhichOneof("payload") == "text_payload"


if __name__ == "__main__":
    pytest_bazel.main()
