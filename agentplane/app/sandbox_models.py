"""Browser HTTP schemas; Sandbox Service itself uses its generated protobuf messages."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentplane.runner.harness import Harness
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.kubernetes_grants import (
    ClusterRoleRef,
    DnsName,
    KubernetesGrant,
    RoleBindingGrant,
    RoleRef,
)
from agentplane.sandbox_service.models import OperatingMode
from agentplane.subjects import ServiceAccountRef
from util.agent_sandbox import SANDBOX_API

# gazelle:include_dep @pypi//protobuf

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=57)]


class KubernetesGrantView(BaseModel):
    """An enabled choice with its fixed target, for the operator's launch picker."""

    model_config = ConfigDict(extra="forbid")

    name: DnsName
    kind: Literal["RoleBinding", "ClusterRoleBinding"]
    namespace: DnsName | None = None
    role_ref: RoleRef | ClusterRoleRef


class ResolvedGrant(BaseModel):
    """Snapshot of the exact choice; catalog changes never retarget an existing Sandbox."""

    model_config = ConfigDict(extra="forbid")

    name: DnsName
    grant: KubernetesGrant


def grant_views(catalog: Mapping[str, KubernetesGrant]) -> list[KubernetesGrantView]:
    return [
        KubernetesGrantView(
            name=name,
            kind=grant.kind,
            namespace=grant.namespace if isinstance(grant, RoleBindingGrant) else None,
            role_ref=grant.role_ref,
        )
        for name, grant in sorted(catalog.items())
    ]


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

    def proto_json(self, session_id: str) -> dict[str, object]:
        values = self.model_dump(exclude_none=True, exclude={"setup_script"})
        if cwd := values.get("cwd"):
            values["cwd"] = str(cwd).replace("{session_id}", session_id)
        if harness := values.pop("harness", None):
            values["harness"] = str(harness)
        if "reasoning_effort" in values:
            values["reasoningEffort"] = values.pop("reasoning_effort")
        return values


class SandboxBinding(BaseModel):
    """The exact reusable session defaults and bootstrap the Sandbox was created with."""

    model_config = ConfigDict(extra="forbid")

    session_defaults: SessionDefaults | None = None
    bootstrap: str = Field(max_length=65_536)


class NewSandbox(BaseModel):
    """The concrete sandbox choices a caller makes after optionally applying a form preset."""

    model_config = ConfigDict(extra="forbid")

    slug: Slug = Field(description="Human-chosen name stem; a random suffix makes the Sandbox name unique.")
    template: str = Field(min_length=1, description="SandboxTemplate whose Pod and volume shape this Sandbox copies.")
    policies: list[str] = Field(default_factory=list, description="EgressPolicy names to grant.")
    action_policy_sets: list[str] = Field(
        default_factory=list,
        description="ActionPolicySet names to bind; an explicit list, empty included, is bound as given.",
    )
    kubernetes_grants: list[str] = Field(
        default_factory=list, description="Enabled Kubernetes grant names to bind to this Sandbox ServiceAccount."
    )
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
    kubernetes_grants: list[ResolvedGrant]
    kubernetes_grants_ready: bool
    kubernetes_grant_error: str | None
    launch_grants_pending: bool
    deleting: bool = False
    pod: PodView | None = None


def sandbox_view(value: protocol_pb2.Sandbox) -> SandboxView:
    data = MessageToDict(value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    data.setdefault("kubernetes_grant_error", None)
    return SandboxView.model_validate(data)


def sandbox_has_ready_pod(view: SandboxView) -> bool:
    if (
        view.deleting
        or view.operating_mode != OperatingMode.RUNNING
        or view.launch_grants_pending
        or not view.kubernetes_grants_ready
        or view.kubernetes_grant_error is not None
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
        owner.api_version != SANDBOX_API.api_version
        or owner.kind != "Sandbox"
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


def create_request(value: NewSandbox) -> protocol_pb2.CreateSandboxRequest:
    return ParseDict(value.model_dump(mode="json", exclude_none=True), protocol_pb2.CreateSandboxRequest())
