from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp

from devinfra.x.schema_codegen.protobuf import common_pb2, sample_pb2


def sample_proto_json(producer: str) -> str:
    sample = sample_pb2.Sample(
        sample_id="sample-17",
        sequence=9_007_199_254_740_993,
        observed_at=Timestamp(seconds=1_796_127_045, nanos=123_000_000),
        note="",
        metadata=common_pb2.CommonMetadata(producer=producer),
        text_payload="protobuf across languages",
    )
    # Python's generated stubs list declared enum values; ProtoJSON still accepts future numbers.
    json_format.Parse('{"status":37}', sample)
    return json_format.MessageToJson(sample)
