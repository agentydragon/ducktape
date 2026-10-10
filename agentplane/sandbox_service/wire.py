"""Typed service protobuf boundary; persisted Kubernetes formats are handled separately."""

from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.sandbox_service import protocol_pb2

# gazelle:include_dep @pypi//protobuf


def launch_overrides(request: protocol_pb2.CreateSessionRequest) -> dict[str, object]:
    paths = list(request.override_mask.paths)
    fields = runner_pb2.SessionSpec.DESCRIPTOR.fields_by_name
    if len(set(paths)) != len(paths) or any(path not in fields for path in paths):
        raise ValueError("override_mask must contain unique SessionSpec field names")
    if any(field.name not in paths for field, _ in request.spec.ListFields()):
        raise ValueError("every supplied SessionSpec field must be selected by override_mask")
    # Use JSON field names here to match the existing stored default overlay. The service wire
    # itself carries the native protobuf, including an explicit mask for default/empty values.
    values = MessageToDict(request.spec, always_print_fields_with_no_presence=True)
    return {fields[path].json_name: values[fields[path].json_name] for path in paths}


def create_proto(
    sandbox: protocol_pb2.SandboxDestination,
    idempotency_key: str,
    overrides: dict[str, object],
    setup_script: str | None,
) -> protocol_pb2.CreateSessionRequest:
    fields = runner_pb2.SessionSpec.DESCRIPTOR.fields
    if any(
        field.name != field.json_name and field.name in overrides and field.json_name in overrides for field in fields
    ):
        raise ValueError("a SessionSpec field cannot use both proto and JSON spellings")
    spec = ParseDict(overrides, runner_pb2.SessionSpec())
    paths = [field.name for field in fields if field.name in overrides or field.json_name in overrides]
    request = protocol_pb2.CreateSessionRequest(
        sandbox=sandbox, idempotency_key=idempotency_key, spec=spec, setup_script=setup_script
    )
    request.override_mask.paths.extend(paths)
    return request
