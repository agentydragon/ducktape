"""Validate complete Session launch specs; runners retain specs, bootstrap results, and setup state."""

from pathlib import PurePosixPath

from agentplane.proto import runner_pb2

# gazelle:include_dep @pypi//protobuf


def validate_spec(spec: runner_pb2.SessionSpec) -> None:
    if spec.harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX) or not spec.cwd or not spec.model:
        raise ValueError("a supported harness, cwd, and model are required")
    if not PurePosixPath(spec.cwd).is_absolute():
        raise ValueError("cwd must be absolute")
