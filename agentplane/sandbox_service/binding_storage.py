"""Read the Sandbox binding annotation, accepting the pre-cutover `thread_defaults` spelling."""

import json

from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane.sandbox_service.protocol_pb2 import SandboxBinding

# gazelle:include_dep @pypi//protobuf


# CLEANUP(added 2026-10-10): drop the legacy key, `is_legacy_binding` and its rewrite in
#   `Provisioning.ensure` once no Sandbox on testing or staging carries `thread_defaults`.
_LEGACY_DEFAULTS_KEY = "thread_defaults"


def is_legacy_binding(raw: str) -> bool:
    return _LEGACY_DEFAULTS_KEY in json.loads(raw)


def read_binding(raw: str) -> SandboxBinding:
    data = json.loads(raw)
    if _LEGACY_DEFAULTS_KEY in data:
        data["session_defaults"] = data.pop(_LEGACY_DEFAULTS_KEY)
    return ParseDict(data, SandboxBinding())


def write_binding(binding: SandboxBinding) -> str:
    return json.dumps(
        MessageToDict(binding, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    )
