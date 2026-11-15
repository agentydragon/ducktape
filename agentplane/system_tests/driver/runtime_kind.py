"""Controller-owned runner kind projected into an EnvironmentDescriptor."""

from __future__ import annotations

from enum import StrEnum

# gazelle:include_dep @pypi//protobuf
from google.protobuf.json_format import MessageToDict

from agentplane.environments.v1 import environment_service_pb2


class RunnerCredentialKind(StrEnum):
    POD = "Pod"
    VM = "VM"


def runner_credential_kind(view: environment_service_pb2.EnvironmentDescriptor) -> RunnerCredentialKind | None:
    """Read the controller's accepted runner kind, or None before it binds an incarnation."""
    diagnostics = view.kubernetes_diagnostics
    if not diagnostics.HasField("sandbox_status"):
        return None

    sandbox_status = MessageToDict(diagnostics.sandbox_status)
    if "runnerCredential" not in sandbox_status:
        return None

    credential = sandbox_status["runnerCredential"]
    if not isinstance(credential, dict):
        raise ValueError("sandbox status runnerCredential must be an object")
    kind = credential.get("kind")
    if not isinstance(kind, str):
        raise ValueError("sandbox status runnerCredential.kind must be a string")
    try:
        return RunnerCredentialKind(kind)
    except ValueError as error:
        raise ValueError(f"unknown sandbox status runnerCredential.kind {kind!r}") from error
