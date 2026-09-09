"""Versioned Action Service wire models and narrow adapter contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from x.agentplane.action_service.catalog import ActionIdentity


class PrincipalRole(StrEnum):
    CALLER = "caller"
    OPERATOR = "operator"


class Principal(BaseModel):
    """Identity established by an authentication adapter, never by the request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    subject: str
    role: PrincipalRole

    @property
    def key(self) -> str:
        return f"{self.issuer}:{self.subject}"


CONFIGURED_IDENTITY_ISSUER = "configured-identity"


class ExternalGrantProvenance(BaseModel):
    """Immutable authenticated submission evidence, never part of the caller envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: str
    issuer: str
    client_id: str
    connection_id: UUID
    grant_id: UUID
    revision: int = Field(ge=1)

    def principal(self) -> Principal:
        return Principal(issuer=CONFIGURED_IDENTITY_ISSUER, subject=self.identity_id, role=PrincipalRole.CALLER)


class ActionState(StrEnum):
    DECISION_PENDING = "decision_pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXECUTION_UNKNOWN = "execution_unknown"


class ExecutionState(StrEnum):
    PENDING_DISPATCH = "pending_dispatch"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXECUTION_UNKNOWN = "execution_unknown"


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class UnknownOutcomeReason(StrEnum):
    """Safe, non-identifying codes for why an Execution outcome could not be observed."""

    ADAPTER_OUTCOME_UNKNOWN = "execution_outcome_unknown"
    COORDINATOR_STOPPED = "coordinator_stopped"
    LEASE_EXPIRED = "lease_expired"
    EXECUTOR_LOST = "executor_lost"


class ReconciliationSource(StrEnum):
    """Who authenticated a terminal outcome for an Execution that was `execution_unknown`."""

    LATE_COMPLETION = "late_completion"
    AUTHORITATIVE_STATUS = "authoritative_status"


class ActionRequestInput(BaseModel):
    """The invariant caller envelope; no owner or decision-route branch is accepted."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    origin: dict[str, JsonValue] = Field(default_factory=dict)
    correlation: dict[str, JsonValue] = Field(default_factory=dict)


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    decision_note: str | None = Field(
        default=None,
        max_length=2000,
        description="Human-authored note shared unchanged with caller and operator; not secret.",
    )


class DecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    verdict: Verdict
    provider: str
    issuer: str
    decision_note: str | None = Field(
        max_length=2000,
        description="Human-authored note shared unchanged with caller and operator; absent for provider decisions.",
    )
    reason_code: str | None = Field(
        default=None, description="Bounded provider-authored reason code; absent for a human Decision."
    )
    reason_description: str | None = Field(
        default=None,
        description="Bounded provider-authored explanation, safe for caller/operator projection; absent for a human Decision.",
    )
    idempotency_key: str
    decided_at: datetime


class ExecutionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    state: ExecutionState
    result: JsonValue | None
    error: dict[str, JsonValue] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reconciled_at: datetime | None


class ActionRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    idempotency_key: str
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    origin: dict[str, JsonValue]
    correlation: dict[str, JsonValue]
    caller_principal: str | None
    external_grant: ExternalGrantProvenance | None = None
    state: ActionState
    version: int
    created_at: datetime
    updated_at: datetime
    decision: DecisionView | None
    execution: ExecutionView | None


class ActionEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    state: ActionState
    at: datetime
    actor_principal: str | None = Field(default=None, description="Authenticated cancellation actor; absent otherwise.")


class CancellationOutcome(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_CANCELLED = "already_cancelled"
    ALREADY_FINISHED = "already_finished"
    TOO_LATE = "too_late"


class CancellationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: CancellationOutcome
    request: ActionRequestView


class ExecutionRequest(BaseModel):
    """The immutable payload handed to exactly one executor dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    origin: dict[str, JsonValue]
    correlation: dict[str, JsonValue]
    caller_principal: str
    external_grant: ExternalGrantProvenance | None = None


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ExecutionState
    result: JsonValue | None = None
    error: dict[str, JsonValue] | None = None


class ExecutionClaim(BaseModel):
    """The unguessable bearer proving which dispatch attempt owns one Execution.

    `lease_token` is the authentication artifact for every later worker-originated call
    (heartbeat, completion) about this one `request_id` — the seam a future
    out-of-process worker would present over the wire, not just an in-process handle.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    executor_id: str
    lease_token: UUID
    lease_expires_at: datetime


class ExecutionLease(Protocol):
    """Handle an executor holds for the one Execution it is currently attempting."""

    async def heartbeat(self) -> bool:
        """Renew the lease. False means it is no longer recognized as the owner —
        the external effect may still be in flight and must not be retried."""
        ...


class Executor(Protocol):
    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult: ...


class ProviderVerdict(StrEnum):
    """A synchronous provider's own disposition; distinct from the aggregated Decision Verdict."""

    ALLOW = "allow"
    DENY = "deny"
    NO_OPINION = "no_opinion"


class ProviderOutcome(BaseModel):
    """One provider's authoritative outcome: bounded and safe for the Action audit/projection.

    `reason_description` is a provider-authored explanation, never unrestricted chain of thought.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ProviderVerdict
    reason_code: str = Field(min_length=1, max_length=64)
    reason_description: str | None = Field(default=None, max_length=500)


class DecisionContext(BaseModel):
    """Trusted evaluation input for a DecisionProvider.

    Deliberately excludes `origin`/`correlation`: identity must come only from the authenticated
    caller and, when resolvable, a verified Agent — never inferred from caller-controlled fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    caller_principal: Principal
    agent_identity: str | None = Field(
        default=None, description="Verified Agent identity, when the deployment can resolve one."
    )


class DecisionProvider(Protocol):
    """A synchronous non-human policy adapter; its outcome is authoritative within the provider."""

    @property
    def name(self) -> str: ...

    async def decide(self, context: DecisionContext) -> ProviderOutcome: ...
