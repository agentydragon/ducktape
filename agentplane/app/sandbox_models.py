"""Browser HTTP schemas and their translation to shared Sandbox protobuf messages."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentplane.proto import sandboxes_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_resource import AutosuspendBlockerReason, OperatingMode, SuspendedBy
from agentplane.subjects import ServiceAccountRef

# Mirrors the CEL floor on `spec.idleSuspendAfter` in agentplane/sandboxes/api/types.go; the CRD is the authority.
MIN_IDLE_SUSPEND_AFTER_SECONDS = 60

SandboxName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=57)]
# The Sandbox CRD's bounds on each of `spec.inboxTags`.
InboxTag = Annotated[str, StringConstraints(min_length=1, max_length=63)]


class SandboxPhase(StrEnum):
    """Where the Sandbox is in its lifecycle, as the shared Sandbox inventory derives it."""

    UNSPECIFIED = "Unspecified"
    CREATING = "Creating"
    RUNNING = "Running"
    SUSPENDING = "Suspending"
    SUSPENDED = "Suspended"
    QUIESCING = "Quiescing"
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
    """The bootstrap script selected for the Sandbox."""

    model_config = ConfigDict(extra="forbid")

    bootstrap: str = Field(max_length=65_536)


class NewSandbox(BaseModel):
    """Concrete Sandbox choices; session launch defaults live in the app preset catalog."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName = Field(description="Kubernetes Sandbox CR name, unique in this namespace.")
    template: str = Field(min_length=1, description="SandboxTemplate each of its incarnations starts from.")
    preset: str | None = Field(default=None, description="The preset the form's fields were taken from, if any.")
    policy_sets: list[str] = Field(default_factory=list, description="PolicySet names to bind.")
    inbox_tags: list[InboxTag] = Field(
        default_factory=list, description="Inbox tags it starts with: groups a send grant can target it by."
    )
    bootstrap: str = Field(default="", max_length=65_536, description="Runner initialization script for this Sandbox.")
    wake_on_send: bool = Field(
        default=False, description="Whether an incoming message may resume this Sandbox after suspension."
    )
    idle_suspend_after_seconds: int | None = Field(
        default=None,
        ge=MIN_IDLE_SUSPEND_AFTER_SECONDS,
        description="How long its runner must stay idle before the Sandbox is suspended; null never suspends it.",
    )


class NewSandboxDefaults(BaseModel):
    """What the Sandbox creation form starts with for the lifecycle policies the operator may change per Sandbox."""

    model_config = ConfigDict(extra="forbid")

    wake_on_send: bool = Field(default=False, description="Whether the form starts with wake on message on.")
    idle_suspend_after_seconds: int | None = Field(
        default=None,
        ge=MIN_IDLE_SUSPEND_AFTER_SECONDS,
        description="The idle suspension time the form starts with; null starts it at never.",
    )


class SandboxWakePolicyChange(BaseModel):
    """The user-controlled wake policy, fenced to the Sandbox incarnation the browser displayed."""

    model_config = ConfigDict(extra="forbid")

    uid: UUID
    wake_on_send: bool


class SandboxIdleSuspendChange(BaseModel):
    """The idle suspension time, fenced to the Sandbox incarnation the browser displayed."""

    model_config = ConfigDict(extra="forbid")

    uid: UUID
    idle_suspend_after_seconds: int | None = Field(
        ge=MIN_IDLE_SUSPEND_AFTER_SECONDS,
        description="How long its runner must stay idle before the Sandbox is suspended; null turns it off.",
    )


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


class AutosuspendBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: AutosuspendBlockerReason
    session_id: str | None


class SandboxView(BaseModel):
    """One inventory row: the Sandbox's identity plus what it and its Pod say."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The Sandbox name, and its Pod's; the handle for every operation.")
    uid: UUID = Field(description="The API server's identity of this Sandbox; what an owned binding references.")
    namespace: str
    created_at: datetime
    operating_mode: OperatingMode | None = Field(
        default=None, description="The controller-reported effective mode; null until its first reconciliation."
    )
    status: dict[str, object] | None = None
    service_account: ServiceAccountRef = Field(
        description="The ServiceAccount its Pod runs as, read off the Sandbox: the subject every "
        "egress and action-policy binding names it by."
    )
    binding: SandboxBinding | None = Field(default=None, description="The bootstrap selected for this Sandbox.")
    preset: str | None = Field(default=None, description="The preset it was made from, when it was made from one.")
    wake_on_send: bool = Field(default=False, description="Whether an incoming message may resume this Sandbox.")
    idle_suspend_after_seconds: int | None = Field(
        default=None, description="How long its runner must stay idle before the Sandbox is suspended; null never."
    )
    suspended_by: SuspendedBy | None = Field(
        default=None,
        description="Why the Sandbox is suspended: its runner was idle for `idle_suspend_after_seconds`, "
        "or a requester suspended it. Null while it runs and until the controller reports.",
    )
    idle_suspend_at: datetime | None = Field(
        default=None,
        description="Controller-clock deadline for the next idle suspension attempt; null when no timer runs.",
    )
    autosuspend_blockers: list[AutosuspendBlocker] = Field(
        default_factory=list, description="Live runner facts that currently keep the Sandbox from autosuspending."
    )
    phase: SandboxPhase = Field(
        description="Unspecified means the controller has not reported an effective mode yet. "
        "Quiescing and Tearing down mean deletion was asked for: it seals and waits for its ingress, "
        "then is removed. A Sandbox in either takes no new work and cannot be suspended or resumed."
    )
    pod: PodView | None = None


def sandbox_view(value: sandboxes_pb2.Sandbox) -> SandboxView:
    data = MessageToDict(value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)
    data["phase"] = SandboxPhase[sandboxes_pb2.SandboxPhase.Name(value.phase).removeprefix("SANDBOX_PHASE_")]
    data["autosuspend_blockers"] = [
        {
            "reason": AutosuspendBlockerReason(blocker.reason),
            "session_id": blocker.session_id if blocker.HasField("session_id") else None,
        }
        for blocker in value.autosuspend_blockers
    ]
    if data.pop("idle_suspend_after", None) is not None:
        data["idle_suspend_after_seconds"] = round(value.idle_suspend_after.ToTimedelta().total_seconds())
    return SandboxView.model_validate(data)


def sandbox_is_ready(view: SandboxView) -> bool:
    """Whether the Sandbox Controller reports its incarnation able to take work: the Sandbox's own `Ready`
    condition and its effective mode, so no caller reads into the incarnation or infers mode from desired state."""
    if view.operating_mode is not OperatingMode.RUNNING:
        return False
    conditions = (view.status or {}).get("conditions")
    return isinstance(conditions, list) and any(
        isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def create_request(value: NewSandbox) -> sandboxes_pb2.CreateSandboxRequest:
    request = ParseDict(
        value.model_dump(mode="json", exclude_none=True, exclude={"idle_suspend_after_seconds"}),
        sandboxes_pb2.CreateSandboxRequest(),
    )
    if value.idle_suspend_after_seconds is not None:
        request.idle_suspend_after.FromSeconds(value.idle_suspend_after_seconds)
    return request


class HarnessProcessView(BaseModel):
    process_id: str
    pid: int | None = None
    harness: Harness
    state: Literal["PROCESS_STATE_STARTING", "PROCESS_STATE_RUNNING", "PROCESS_STATE_STOPPING"]
    assigned_session_ids: list[str]


class RunnerSessionView(BaseModel):
    session_id: str
    process_id: str | None = None
    native_session_id: str | None = None
    execution_state: Literal[
        "SESSION_EXECUTION_STATE_DORMANT",
        "SESSION_EXECUTION_STATE_PREPARING",
        "SESSION_EXECUTION_STATE_READY",
        "SESSION_EXECUTION_STATE_FAILED",
    ]
    active_turn_id: str | None = None
    failure_cursor: str | None = None
    busy: bool


class RunnerLifecycleBlockerView(BaseModel):
    kind: Literal[
        "RUNNER_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_ADMISSIONS",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_HALT",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL",
        "RUNNER_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT",
    ]
    error: str
    resource_type: str
    resource_id: str
    process_id: str
    resource_present: bool
    admissions: str
    journal_seal_present: bool
    journal_seal_uid: str
    expected_journal_seal_uid: str
    journal_seal_cursor: str
    acknowledgement_present: bool
    acknowledgement_sealed: bool
    acknowledgement_cursor: str
    finalizers_pending: bool


class RunnerLifecycleView(BaseModel):
    desired_mode: Literal[
        "RUNNER_LIFECYCLE_MODE_UNSPECIFIED",
        "RUNNER_LIFECYCLE_MODE_RUNNING",
        "RUNNER_LIFECYCLE_MODE_QUIESCING",
        "RUNNER_LIFECYCLE_MODE_CLOSING",
    ]
    desired_generation: str
    progress_generation: str
    progress_available: bool
    progress_current: bool
    stage: Literal[
        "RUNNER_LIFECYCLE_STAGE_UNSPECIFIED",
        "RUNNER_LIFECYCLE_STAGE_RUNNING",
        "RUNNER_LIFECYCLE_STAGE_DRAINING",
        "RUNNER_LIFECYCLE_STAGE_HALTING",
        "RUNNER_LIFECYCLE_STAGE_STOPPING",
        "RUNNER_LIFECYCLE_STAGE_FENCING",
        "RUNNER_LIFECYCLE_STAGE_SEALING",
        "RUNNER_LIFECYCLE_STAGE_FLUSHING",
        "RUNNER_LIFECYCLE_STAGE_QUIESCED",
        "RUNNER_LIFECYCLE_STAGE_CLOSED",
    ]
    error: str
    blocker: RunnerLifecycleBlockerView | None = None


class RunnerStatusView(BaseModel):
    runner_boot_id: str
    processes: list[HarnessProcessView]
    sessions: list[RunnerSessionView]
    lifecycle: RunnerLifecycleView | None = None
