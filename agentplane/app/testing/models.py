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
from agentplane.policy_sets import ConditionStatus
from agentplane.runner.harness import Harness
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


class ThreadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads: list[ThreadView]
    next_page_token: str | None


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
