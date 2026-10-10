"""The Sandbox binding annotation's JSON form."""

import json

from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane.sandbox_service.protocol_pb2 import SandboxBinding

# gazelle:include_dep @pypi//protobuf


def read_binding(raw: str) -> SandboxBinding:
    return ParseDict(json.loads(raw), SandboxBinding())


def write_binding(binding: SandboxBinding) -> str:
    return json.dumps(
        MessageToDict(binding, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    )
