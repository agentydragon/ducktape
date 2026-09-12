"""Versioned Action Service wire models and narrow adapter contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Discriminator, Field, JsonValue, Tag, model_validator

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


SANDBOX_ISSUER = "kubernetes-sandbox"
SERVICE_ACCOUNT_ISSUER = "service-account"
# Rows written before external callers were ServiceAccounts; readable, never authorizing.
CONFIGURED_IDENTITY_ISSUER = "configured-identity"
EXTERNAL_ISSUERS = frozenset({SERVICE_ACCOUNT_ISSUER, CONFIGURED_IDENTITY_ISSUER})


class ServiceAccountRef(BaseModel):
    """A Kubernetes ServiceAccount by namespace and name; as an Action caller it is eligible only while
    labeled, which the Connection authority checks on every resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)

    def principal(self) -> Principal:
        return Principal(
            issuer=SERVICE_ACCOUNT_ISSUER, subject=f"{self.namespace}:{self.name}", role=PrincipalRole.CALLER
        )


class ConfiguredIdentityRef(BaseModel):
    """The subject of a grant bound before external callers were ServiceAccounts. Nothing resolves
    it any more: such a Connection stays readable in the inventory and on its Actions' provenance,
    and fresh OAuth selecting a ServiceAccount is how it regains authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: str

    def principal(self) -> Principal:
        return Principal(issuer=CONFIGURED_IDENTITY_ISSUER, subject=self.identity_id, role=PrincipalRole.CALLER)


def _grant_caller_kind(value: Any) -> str | None:
    if isinstance(value, ConfiguredIdentityRef) or (isinstance(value, dict) and "identity_id" in value):
        return "configured_identity"
    if isinstance(value, ServiceAccountRef | dict):
        return "service_account"
    return None


GrantCaller = Annotated[
    Annotated[ServiceAccountRef, Tag("service_account")] | Annotated[ConfiguredIdentityRef, Tag("configured_identity")],
    Discriminator(_grant_caller_kind),
]


class SandboxCaller(BaseModel):
    """The live Sandbox proven by workload authentication, as an ActionPolicyBinding names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = Field(min_length=1)
    sandbox_uid: str = Field(min_length=1)

    def principal(self) -> Principal:
        return Principal(
            issuer=SANDBOX_ISSUER, subject=f"{self.namespace}:{self.sandbox_uid}", role=PrincipalRole.CALLER
        )

    @classmethod
    def from_principal(cls, principal: Principal) -> SandboxCaller:
        """The inverse of `principal()`; only a principal workload authentication minted decodes."""
        namespace, separator, sandbox_uid = principal.subject.partition(":")
        if (
            principal.issuer != SANDBOX_ISSUER
            or principal.role is not PrincipalRole.CALLER
            or not separator
            or not namespace
            or not sandbox_uid
            or ":" in sandbox_uid
        ):
            raise ValueError("principal was not minted by Sandbox workload authentication")
        return cls(namespace=namespace, sandbox_uid=sandbox_uid)


class ServiceAccountCaller(BaseModel):
    """An external Connection acting as a labeled ServiceAccount through one active grant revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service_account: ServiceAccountRef
    grant_revision: int = Field(ge=1)

    def principal(self) -> Principal:
        return self.service_account.principal()


class PolicyKind(StrEnum):
    """The `type` of one ActionPolicySet policy; each names one Python evaluator."""

    EXACT_ACTIONS = "exact_actions"
    ARGUMENT_SCHEMA = "argument_schema"


class BindingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str
    name: str
    resource_version: str


class PolicySetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str
    name: str
    generation: int


class MatchedPolicy(BaseModel):
    """The leaf policy that produced the Decision: which set, which list, which entry, which kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str
    policy_set: str
    source: Literal["autoApproveIf"]
    index: int = Field(ge=0)
    type: PolicyKind


class PolicyEvidence(BaseModel):
    """What a policy-set Decision evaluated, as the objects stood at admission, so the Decision still
    explains itself after they change: every binding the caller had and its resourceVersion, every set
    those bindings resolved and its generation, and the policy that matched."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bindings: list[BindingEvidence]
    policy_sets: list[PolicySetEvidence]
    matched: MatchedPolicy


class ExternalGrantProvenance(BaseModel):
    """Immutable authenticated submission evidence, never part of the caller envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    caller: GrantCaller
    issuer: str
    client_id: str
    connection_id: UUID
    grant_id: UUID
    revision: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def _legacy_identity(cls, value: Any) -> Any:
        # Snapshots persisted before ServiceAccount callers carry the configured Identity at top level.
        if isinstance(value, dict) and "identity_id" in value and "caller" not in value:
            legacy = dict(value)
            legacy["caller"] = {"identity_id": legacy.pop("identity_id")}
            return legacy
        return value

    def principal(self) -> Principal:
        return self.caller.principal()


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
    policy_evidence: PolicyEvidence | None = Field(
        default=None, description="The objects a policy-set Decision evaluated; absent for every other Decision."
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

    @property
    def renewal_interval(self) -> timedelta:
        """Maximum interval and RPC timeout for renewals, each below half the lease window."""
        ...

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
    evidence: PolicyEvidence | None = Field(
        default=None, description="Recorded on the Decision when the provider decided from policy objects."
    )
