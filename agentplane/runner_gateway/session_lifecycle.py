"""Explicit session management; runners retain launch specs, bootstrap results, and setup state."""

from pathlib import PurePosixPath

from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane.proto import runner_pb2
from agentplane.proto.sandbox_service_pb2 import SandboxBinding

# gazelle:include_dep @pypi//protobuf


def launch_spec(
    session_id: str, overrides: dict[str, object], *, binding: SandboxBinding | None
) -> runner_pb2.SessionSpec:
    defaults = binding.session_defaults if binding is not None and binding.HasField("session_defaults") else None
    values = MessageToDict(defaults) if defaults is not None else {}
    values.pop("setupScript", None)
    merged = values | overrides
    if isinstance(cwd := merged.get("cwd"), str):
        merged["cwd"] = cwd.replace("{session_id}", session_id)
    spec = ParseDict(merged, runner_pb2.SessionSpec())
    validate_spec(spec)
    return spec


def validate_spec(spec: runner_pb2.SessionSpec) -> None:
    if spec.harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX) or not spec.cwd or not spec.model:
        raise ValueError("a supported harness, cwd, and model are required")
    if not PurePosixPath(spec.cwd).is_absolute():
        raise ValueError("cwd must be absolute")
