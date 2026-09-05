"""Versioned Action Service wire models and narrow adapter contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


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


class ActionRequestInput(BaseModel):
    """The invariant caller envelope; no owner or decision-route branch is accepted."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    capability: str = Field(min_length=1, max_length=240)
    arguments: dict[str, JsonValue]
    origin: dict[str, JsonValue] = Field(default_factory=dict)
    correlation: dict[str, JsonValue] = Field(default_factory=dict)


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    private_reason: str | None = Field(default=None, max_length=2000)


class DecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    verdict: Verdict
    provider: str
    issuer: str
    private_reason: str | None
    private_reason_redacted: bool
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


class ActionRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    idempotency_key: str
    capability: str
    arguments: dict[str, JsonValue]
    origin: dict[str, JsonValue]
    correlation: dict[str, JsonValue]
    caller_principal: str | None
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


class ExecutionRequest(BaseModel):
    """The immutable payload handed to exactly one executor dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    capability: str
    arguments: dict[str, JsonValue]
    origin: dict[str, JsonValue]
    correlation: dict[str, JsonValue]
    caller_principal: str


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ExecutionState
    result: JsonValue | None = None
    error: dict[str, JsonValue] | None = None


class Executor(Protocol):
    @property
    def capabilities(self) -> frozenset[str]: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class NotificationOutbox(Protocol):
    """A delivery adapter may drain durable outbox rows; it never decides a request."""

    async def wake(self) -> None: ...


class NullNotificationOutbox:
    async def wake(self) -> None:
        return None
