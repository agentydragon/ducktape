"""HTTP contract models used by the app's independent clients and system tests."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from agentplane.action_service.policy_view import EffectivePolicyView
from agentplane.app.sandbox_models import MIN_IDLE_SUSPEND_AFTER_SECONDS, InboxTag
from agentplane.egress.decisions import Decision
from agentplane.egress.resources import Target
from agentplane.env.harnesshost.harness import Harness
from agentplane.policy_sets import ConditionStatus
from agentplane.sandbox_resource import AutosuspendBlockerReason, OperatingMode, SuspendedBy
from agentplane.subjects import ServiceAccountRef

SUBSET_ROW_LIMIT = 200

SandboxName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", min_length=1, max_length=57)]


class NewSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    template: str = Field(min_length=1)
    preset: str | None = None
    policy_sets: list[str] = Field(default_factory=list)
    inbox_tags: list[InboxTag] = Field(default_factory=list)
    bootstrap: str = Field(default="", max_length=65_536)
    wake_on_send: bool = False
    idle_suspend_after_seconds: int | None = Field(default=None, ge=MIN_IDLE_SUSPEND_AFTER_SECONDS)


class SandboxPhase(StrEnum):
    UNSPECIFIED = "Unspecified"
    CREATING = "Creating"
    RUNNING = "Running"
    SUSPENDING = "Suspending"
    SUSPENDED = "Suspended"
    QUIESCING = "Quiescing"
    TEARING_DOWN = "Tearing down"


class SandboxBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap: str


class CredentialView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    placeholder: str
    secret: str | None = None
    key: str | None = None
    targets: list[Target]


class EgressRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["egress"]
    number: int
    hosts: list[str]
    methods: list[str] | None = None
    paths: list[str] | None = None
    credential: CredentialView | None = None
    missing_credential: str | None = None


class OtherRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["other"]
    number: int
    type: str
    fields: dict[str, JsonValue]


RuleView = Annotated[EgressRuleView | OtherRuleView, Field(discriminator="kind")]


class PolicySetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    generation: int
    rules: list[RuleView]
    refused: str | None = None


class BindingProvenance(StrEnum):
    GIT = "git"
    SANDBOX = "sandbox"
    OPERATOR = "operator"


class ConditionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    status: ConditionStatus
    reason: str
    message: str
    observed_generation: int | None = None


class BindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provenance: BindingProvenance
    generation: int
    subjects: list[ServiceAccountRef]
    expires_at: datetime | None = None
    conditions: list[ConditionView]
    policy_sets: list[PolicySetView]
    missing_policy_sets: list[str]


class OwnerReferenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str
    kind: str
    name: str
    uid: str
    controller: bool


class PodView(BaseModel):
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
    model_config = ConfigDict(extra="forbid")

    name: str
    uid: UUID
    namespace: str
    created_at: datetime
    operating_mode: OperatingMode | None = None
    status: dict[str, object] | None = None
    service_account: ServiceAccountRef
    binding: SandboxBinding | None = None
    preset: str | None = None
    wake_on_send: bool = False
    idle_suspend_after_seconds: int | None = None
    suspended_by: SuspendedBy | None = None
    idle_suspend_at: datetime | None = None
    autosuspend_blockers: list[AutosuspendBlocker] = Field(default_factory=list)
    phase: SandboxPhase
    pod: PodView | None = None


def sandbox_is_ready(view: SandboxView) -> bool:
    """Whether the public Sandbox view reports a running incarnation with a Ready condition."""
    if view.operating_mode is not OperatingMode.RUNNING:
        return False
    conditions = (view.status or {}).get("conditions")
    return isinstance(conditions, list) and any(
        isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


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


class EnvironmentRuntimeLifecycleBlockerView(BaseModel):
    kind: Literal[
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_UNSPECIFIED",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_ADMISSIONS",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_HALT",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_PROCESS",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_APP_SERVER",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_CODEX_EXEC_SERVER",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_BOOTSTRAP",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_PROCESS_CLEANUP",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_INSPECTION",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_JOURNAL_SEAL",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT",
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


class EnvironmentRuntimeLifecycleView(BaseModel):
    desired_mode: Literal[
        "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_UNSPECIFIED",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_CLOSING",
    ]
    desired_generation: str
    progress_generation: str
    progress_available: bool
    progress_current: bool
    stage: Literal[
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_UNSPECIFIED",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_DRAINING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_HALTING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_STOPPING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FENCING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_SEALING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED",
        "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_CLOSED",
    ]
    error: str
    blocker: EnvironmentRuntimeLifecycleBlockerView | None = None


class EnvironmentRuntimeStatusView(BaseModel):
    environment_runtime_boot_id: str
    processes: list[HarnessProcessView]
    sessions: list[RunnerSessionView]
    lifecycle: EnvironmentRuntimeLifecycleView | None = None


class SessionDefaults(BaseModel):
    """Session launch defaults returned by the Go app's preset endpoint."""

    model_config = ConfigDict(extra="forbid")

    harness: Harness | None = None
    model: str | None = None
    cwd: str | None = None
    reasoning_effort: str | None = None
    instructions: str | None = None
    setup_script: str | None = Field(default=None, max_length=65_536)


class SandboxPresetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    template: str
    policy_sets: list[str]
    inbox_tags: list[str]
    session_defaults: SessionDefaults
    bootstrap: str


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, object]
    session_id: UUID
    sandbox_uid: UUID
    setup_script: str | None = Field(default=None, max_length=65_536)


class ThreadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sandbox: str
    sandbox_uid: UUID | None = None
    harness: Harness | None
    model: str | None
    cwd: str | None
    created_at: datetime
    name: str | None
    archived: bool
    last_cursor: int = Field(description="The Go API serializes this uint64 as a decimal integer or string.")
    last_event_at: datetime | None = None
    last_model_activity_at: datetime | None = None
    idle_since: datetime | None = None
    harness_state: str
    setup_state: str | None = None
    launch_outcome: str | None = None
    active_turn_id: str | None = None
    reasoning_effort: str | None = None
    feed_status: Literal["active", "ended", "failed"] | None = None
    last_turn_status: str | None = None


class ThreadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID = Field(validation_alias="threadId")
    created_at: datetime = Field(validation_alias="createdAt")
    name: str | None = None
    archived: bool = False

    @property
    def id(self) -> UUID:
        return self.thread_id


def thread_view_from_proto_json(metadata: dict[str, object], summary: dict[str, object]) -> ThreadView:
    """Compose the generated App Thread metadata and History summary for test clients."""
    attached = summary.get("attached")
    attachment = attached if isinstance(attached, dict) else {}
    spec_value = attachment.get("spec")
    spec = spec_value if isinstance(spec_value, dict) else {}
    sandbox_value = summary.get("sandbox")
    sandbox = sandbox_value if isinstance(sandbox_value, dict) else {}
    feed_statuses = {"FEED_STATUS_ACTIVE": "active", "FEED_STATUS_ENDED": "ended", "FEED_STATUS_FAILED": "failed"}
    feed_status = feed_statuses.get(str(summary.get("feedStatus"))) if attached is not None else None
    return ThreadView.model_validate(
        {
            "id": metadata["threadId"],
            "sandbox": sandbox.get("name", ""),
            "sandbox_uid": sandbox.get("uid"),
            "harness": spec.get("harness"),
            "model": spec.get("model"),
            "cwd": spec.get("cwd"),
            "created_at": metadata["createdAt"],
            "name": metadata.get("name"),
            "archived": metadata.get("archived", False),
            "last_cursor": int(str(summary.get("foldedThrough", "0"))),
            "last_event_at": summary.get("lastEventAt"),
            "last_model_activity_at": summary.get("lastModelActivityAt"),
            "idle_since": summary.get("idleSince"),
            "harness_state": attachment.get("harnessState", "HARNESS_STATE_UNSPECIFIED"),
            "setup_state": attachment.get("setupState"),
            "launch_outcome": attachment.get("launchOutcome"),
            "active_turn_id": attachment.get("activeTurnId"),
            "reasoning_effort": spec.get("reasoningEffort"),
            "feed_status": feed_status,
            "last_turn_status": summary.get("lastTurnStatus"),
        }
    )


class KindFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    since: datetime


class WatchHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    healthy: bool
    failing: dict[str, KindFailure]


class SandboxesSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandboxes: list[SandboxView]
    watch: WatchHealth


class DecisionsUnavailable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str


class DecisionsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watching: bool
    decisions: list[Decision] | DecisionsUnavailable


class ThreadScopeResponse(BaseModel):
    projection_epoch: str
    through_cursor: str


class SubsetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    where: str | None = None
    params: dict[str, str] | None = None
    order_by: str | None = None
    limit: int | None = None


class ActionPolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    synced: bool
    auto_approve_if: list[EffectivePolicyView]


class ActionUpstreamFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    url: str
    upstream_status: int | None
    error_type: str


class ActionPolicyUnavailable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["unavailable"]
    code: str
    upstream: ActionUpstreamFailure | None = None


class CallerGrantView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: list[BindingView]
    action_policy: ActionPolicyView | ActionPolicyUnavailable
