"""Explicit session management; runners retain launch specs, bootstrap results, and setup state."""

import json
from pathlib import PurePosixPath
from uuid import UUID

from google.protobuf.json_format import MessageToDict, MessageToJson, ParseDict

from agentplane.proto import runner_control_pb2 as control_pb2, runner_pb2
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SessionDestination
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway.instructions import combine_instructions
from agentplane.runner_gateway.lifecycle_control import LifecycleControl

# gazelle:include_dep @pypi//protobuf


def launch_spec(
    destination: SessionDestination,
    overrides: dict[str, object],
    *,
    binding: SandboxBinding | None,
    platform_instructions: str,
    sandbox_namespace: str,
) -> runner_pb2.SessionSpec:
    defaults = binding.session_defaults if binding is not None and binding.HasField("session_defaults") else None
    values = MessageToDict(defaults) if defaults is not None else {}
    values.pop("setupScript", None)
    merged = values | overrides
    if isinstance(cwd := merged.get("cwd"), str):
        merged["cwd"] = cwd.replace("{session_id}", destination.session_id)
    spec = ParseDict(merged, runner_pb2.SessionSpec())
    validate_spec(spec)
    context = (
        "Your explicit Runner Gateway session destination is:\n"
        f"{MessageToJson(destination, preserving_proto_field_name=True)}\n"
        "Use these identifiers when addressing this session; they are not credentials."
    )
    context += "\nYour explicit notification destination is:\n" + json.dumps(
        {
            "destination_ref": {
                "namespace": sandbox_namespace,
                "name": destination.sandbox.sandbox,
                "uid": destination.sandbox.sandbox_uid,
            },
            "session_id": destination.session_id,
        }
    )
    spec.instructions = combine_instructions(combine_instructions(platform_instructions, context), spec.instructions)
    return spec


def validate_spec(spec: runner_pb2.SessionSpec) -> None:
    if spec.harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX) or not spec.cwd or not spec.model:
        raise ValueError("a supported harness, cwd, and model are required")
    if not PurePosixPath(spec.cwd).is_absolute():
        raise ValueError("cwd must be absolute")


async def open_session(
    control: LifecycleControl,
    sandbox_uid: UUID,
    runner_session_id: str,
    spec: runner_pb2.SessionSpec,
    *,
    bootstrap: str,
    setup_script: str | None,
) -> runner_pb2.Attached:
    """Run the Sandbox bootstrap once, then create the Session with `spec` and start it."""
    if bootstrap:
        initialize = control_pb2.ControlRequest(initialize=control_pb2.Initialize(script=bootstrap))
        initialized = (await control.request(sandbox_uid, initialize)).initialize
        if initialized.HasField("interrupted"):
            raise RunnerError("the Sandbox bootstrap was interrupted and never runs again; use a new Sandbox")
        if initialized.result.exit_code != 0:
            raise RunnerError("Sandbox bootstrap failed; no session was opened")
    opening = control_pb2.OpenSession(session_id=runner_session_id, spec=spec)
    if setup_script is not None:
        opening.setup_script = setup_script
    attached = (await control.request(sandbox_uid, control_pb2.ControlRequest(open=opening))).open
    # OpenSession only confirms an existing Session, so a retried create would leave a harness that
    # an earlier attempt could not start (refused FAILED) stopped. Start it; a Session still in setup
    # starts once its setup succeeds.
    if attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED and attached.setup_state in (
        runner_pb2.SETUP_STATE_NOT_REQUIRED,
        runner_pb2.SETUP_STATE_SUCCEEDED,
    ):
        return await resume_session(control, sandbox_uid, runner_session_id)
    return attached


async def resume_session(control: LifecycleControl, sandbox_uid: UUID, runner_session_id: str) -> runner_pb2.Attached:
    """Restart a stopped harness on its retained spec; the runner refuses an unknown Session or one
    whose setup failed."""
    resuming = control_pb2.ControlRequest(resume=control_pb2.ResumeSession(session_id=runner_session_id))
    return (await control.request(sandbox_uid, resuming)).resume
