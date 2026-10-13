"""Browser HTTP schemas; Sandbox Service itself uses its generated protobuf messages."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentplane.proto import sandbox_service_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND, OperatingMode
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf

SandboxName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=57)]


class SandboxPhase(StrEnum):
    """Where the Sandbox is in its lifecycle, as the Sandbox Service derives it."""

    CREATING = "Creating"
    RUNNING = "Running"
    SUSPENDING = "Suspending"
    SUSPENDED = "Suspended"
    SEALING = "Sealing"
    TEARING_DOWN = "Tearing down"


class SessionDefaults(BaseModel):
    """Editable session launch fields; null means the caller deliberately left that field unspecified."""

    model_config = ConfigDict(extra="forbid")

    harness: Harness | None = None
    model: str | None = None
    cwd: str | None = None
    reasoning_effort: str | None = None
    instructions: str | None = None
    setup_script: str | None = Field(default=None, max_length=65_536)

    def over(self, base: SessionDefaults) -> SessionDefaults:
        """Replace only fields explicitly present in this object, including an explicit empty string."""
        return base.model_copy(update=self.model_dump(exclude_none=True))


class SandboxBinding(BaseModel):
    """The exact reusable session defaults and bootstrap the Sandbox was created with."""

    model_config = ConfigDict(extra="forbid")

    session_defaults: SessionDefaults | None = None
    bootstrap: str = Field(max_length=65_536)


class NewSandbox(BaseModel):
    """The concrete sandbox choices a caller makes after optionally applying a form preset."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName = Field(description="Kubernetes Sandbox CR name, unique in this namespace.")
    template: str = Field(min_length=1, description="SandboxTemplate each of its incarnations starts from.")
    policy_sets: list[str] = Field(default_factory=list, description="PolicySet names to bind.")
    session_defaults: SessionDefaults | None = Field(
        default=None, description="Reusable session defaults for future sessions in this Sandbox."
    )
    bootstrap: str = Field(default="", max_length=65_536, description="Runner initialization script for this Sandbox.")


class OwnerReferenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str
    kind: str
    name: str
    uid: str
    controller: bool


class PodView(BaseModel):
    """The owned Pod's identity metadata and the kubelet's unmodified status object."""

    model_config = ConfigDict(extra="forbid")

    name: str
    namespace: str
    uid: str
    deleting: bool
    owner_references: list[OwnerReferenceView]
    node_name: str | None = None
    status: dict[str, object] | None = None


class SandboxView(BaseModel):
    """One inventory row: the Sandbox's identity plus what it and its Pod say."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The Sandbox name, and its Pod's; the handle for every operation.")
    uid: UUID = Field(description="The API server's identity of this Sandbox; what an owned binding references.")
    namespace: str
    created_at: datetime
    operating_mode: OperatingMode
    status: dict[str, object] | None = None
    service_account: ServiceAccountRef = Field(
        description="The ServiceAccount its Pod runs as, read off the Sandbox: the subject every "
        "egress and action-policy binding names it by."
    )
    binding: SandboxBinding | None = Field(
        default=None, description="The stored concrete session defaults and bootstrap selected for this Sandbox."
    )
    phase: SandboxPhase = Field(
        description="Sealing and Tearing down mean deletion was asked for: it seals and waits for its ingress, "
        "then is removed. A Sandbox in either takes no new work and cannot be suspended or resumed."
    )
    pod: PodView | None = None


def sandbox_view(value: sandbox_service_pb2.Sandbox) -> SandboxView:
    data = MessageToDict(value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    data["phase"] = SandboxPhase[sandbox_service_pb2.SandboxPhase.Name(value.phase).removeprefix("SANDBOX_PHASE_")]
    return SandboxView.model_validate(data)


def sandbox_has_ready_pod(view: SandboxView) -> bool:
    if (
        view.phase != SandboxPhase.RUNNING
        or view.pod is None
        or view.pod.name != view.name
        or view.pod.namespace != view.namespace
        or not view.pod.uid
        or view.pod.deleting
    ):
        return False
    owners = [owner for owner in view.pod.owner_references if owner.controller]
    if len(owners) != 1:
        return False
    owner = owners[0]
    if (
        owner.api_version != SANDBOX_API_VERSION
        or owner.kind != SANDBOX_KIND
        or owner.name != view.name
        or owner.uid != str(view.uid)
    ):
        return False
    status = view.pod.status
    if status is None or status.get("phase") != "Running" or not status.get("podIP"):
        return False
    conditions = status.get("conditions")
    return isinstance(conditions, list) and any(
        isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def create_request(value: NewSandbox) -> sandbox_service_pb2.CreateSandboxRequest:
    return ParseDict(value.model_dump(mode="json", exclude_none=True), sandbox_service_pb2.CreateSandboxRequest())
