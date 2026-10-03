"""Explicit session management; runners retain launch specs, bootstrap results, and setup state."""

import json
from pathlib import PurePosixPath

from google.protobuf.json_format import MessageToDict, MessageToJson, ParseDict

from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import RunnerError
from agentplane.sandbox_service.instructions import combine_instructions
from agentplane.sandbox_service.kind import from_wire
from agentplane.sandbox_service.models import EnvironmentKind
from agentplane.sandbox_service.protocol_pb2 import SandboxBinding, SessionDestination

# gazelle:include_dep @pypi//protobuf


def launch_spec(
    destination: SessionDestination,
    overrides: dict[str, object],
    *,
    binding: SandboxBinding | None,
    platform_instructions: str,
    sandbox_namespace: str | None = None,
) -> protocol_pb2.SessionSpec:
    defaults = binding.session_defaults if binding is not None and binding.HasField("session_defaults") else None
    values = MessageToDict(defaults) if defaults is not None else {}
    values.pop("setupScript", None)
    if "cwd" in values:
        values["cwd"] = values["cwd"].replace("{session_id}", destination.session_id)
    spec = ParseDict(values | overrides, protocol_pb2.SessionSpec())
    validate_spec(spec)
    path = PurePosixPath(spec.cwd)
    if from_wire(destination.sandbox.kind) == EnvironmentKind.KUBEVIRT and (
        ".." in path.parts or path == PurePosixPath("/workspace") or not path.is_relative_to("/workspace")
    ):
        raise ValueError("VM session cwd must be a child of /workspace")
    context = (
        "Your explicit Sandbox Service session destination is:\n"
        f"{MessageToJson(destination, preserving_proto_field_name=True)}\n"
        "Use these identifiers when addressing this session; they are not credentials."
    )
    if sandbox_namespace is not None:
        context += "\nYour explicit notification destination is:\n" + json.dumps(
            {
                "destination_ref": {
                    "namespace": sandbox_namespace,
                    "name": destination.sandbox.sandbox,
                    "uid": destination.sandbox.sandbox_uid,
                    "kind": from_wire(destination.sandbox.kind).value,
                },
                "session_id": destination.session_id,
            }
        )
    spec.instructions = combine_instructions(combine_instructions(platform_instructions, context), spec.instructions)
    return spec


def validate_spec(spec: protocol_pb2.SessionSpec) -> None:
    if spec.harness not in (protocol_pb2.HARNESS_CLAUDE, protocol_pb2.HARNESS_CODEX) or not spec.cwd or not spec.model:
        raise ValueError("a supported harness, cwd, and model are required")
    if not PurePosixPath(spec.cwd).is_absolute():
        raise ValueError("cwd must be absolute")


async def open_session(
    client: RunnerClient,
    destination: SessionDestination,
    spec: protocol_pb2.SessionSpec,
    *,
    binding: SandboxBinding | None,
    setup_script: str | None,
) -> protocol_pb2.Attached:
    if binding is not None:
        if binding.bootstrap:
            result = await client.initialize(binding.bootstrap)
            if result.exit_code != 0:
                raise RunnerError("Sandbox bootstrap failed; no session was opened")
        if setup_script is None and binding.session_defaults.HasField("setup_script"):
            setup_script = binding.session_defaults.setup_script
    attachment = await client.attach(destination.session_id, spec=spec, setup_script=setup_script)
    try:
        return attachment.attached
    finally:
        attachment.cancel()


async def resume_session(client: RunnerClient, session_id: str) -> protocol_pb2.Attached:
    # Never assemble a new spec from today's configuration. No retained session means no resume.
    summary = next((row for row in await client.list_sessions() if row.session_id == session_id), None)
    if summary is None:
        raise RunnerError("runner has no retained session to resume")
    if summary.setup_state in (protocol_pb2.SETUP_STATE_FAILED, protocol_pb2.SETUP_STATE_INTERRUPTED):
        raise RunnerError("session setup failed or was interrupted; create a new session")
    try:
        validate_spec(summary.spec)
    except ValueError as error:
        raise RunnerError("runner session has no recoverable spec") from error
    attachment = await client.attach(session_id, spec=summary.spec)
    try:
        return attachment.attached
    finally:
        attachment.cancel()
