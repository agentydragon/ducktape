"""Build the Runner Gateway's CreateSession request from a complete launch spec."""

from uuid import UUID

from google.protobuf.json_format import ParseDict

from agentplane.proto import runner_gateway_pb2, runner_pb2, sandboxes_pb2

# gazelle:include_dep @pypi//protobuf


def create_proto(
    sandbox: sandboxes_pb2.SandboxDestination, session_id: UUID, spec: dict[str, object], setup_script: str | None
) -> runner_gateway_pb2.CreateSessionRequest:
    fields = runner_pb2.SessionSpec.DESCRIPTOR.fields
    if any(field.name != field.json_name and field.name in spec and field.json_name in spec for field in fields):
        raise ValueError("a SessionSpec field cannot use both proto and JSON spellings")
    launch_spec = ParseDict(spec, runner_pb2.SessionSpec())
    return runner_gateway_pb2.CreateSessionRequest(
        sandbox=sandbox, session_id=str(session_id), spec=launch_spec, setup_script=setup_script
    )
