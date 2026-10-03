"""Explicit conversion between domain environment identities and the protobuf enum."""

from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.models import EnvironmentKind, environment_kind


def to_wire(value: str) -> protocol_pb2.EnvironmentKind:
    kind = environment_kind(value)
    if kind == EnvironmentKind.AGENT_SANDBOX:
        return protocol_pb2.ENVIRONMENT_KIND_AGENT_SANDBOX
    if kind == EnvironmentKind.KUBEVIRT:
        return protocol_pb2.ENVIRONMENT_KIND_KUBEVIRT
    raise AssertionError(f"unhandled environment kind {kind}")


def from_wire(value: int) -> EnvironmentKind:
    if value == protocol_pb2.ENVIRONMENT_KIND_AGENT_SANDBOX:
        return EnvironmentKind.AGENT_SANDBOX
    if value == protocol_pb2.ENVIRONMENT_KIND_KUBEVIRT:
        return EnvironmentKind.KUBEVIRT
    raise ValueError(f"unsupported environment kind {value}")


# gazelle:include_dep @pypi//protobuf
