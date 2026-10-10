"""Read the Sandbox binding annotation, accepting the pre-cutover `thread_defaults` spelling."""

import json

from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane.sandbox_service.protocol_pb2 import SandboxBinding

# gazelle:include_dep @pypi//protobuf


def read_binding(raw: str) -> SandboxBinding:
    data = json.loads(raw)
    # CLEANUP(added 2026-10-10): drop this translation once a Sandbox Service release that writes
    #   `session_defaults` has rolled out to staging (SANDBOX_BINDING_ANNOTATION_RENAME).
    if "thread_defaults" in data:
        data["session_defaults"] = data.pop("thread_defaults")
    return ParseDict(data, SandboxBinding())


def write_binding(binding: SandboxBinding) -> str:
    return json.dumps(
        MessageToDict(binding, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    )
