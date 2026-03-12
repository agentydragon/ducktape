"""Data models for Airlock.

All state variants are discriminated unions — no nullable result fields at the top level.
Invalid states are unrepresentable by construction.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

# ── The underlying MCP tool call ──────────────────────────────────────────────


class ToolCall(BaseModel):
    """The underlying MCP tool call to forward on approval.

    justification is stripped before storage here; it lives on Action directly.
    """

    server_namespace: str
    tool_name: str
    arguments: dict[str, object]

    model_config = ConfigDict(extra="forbid")


# ── Compound action key ─────────────────────────────────────────────────────


class ActionKey(BaseModel):
    """Compound action identifier: (session_key, action_seq).

    action_seq is 1-based and monotonically increasing per session_key.
    """

    session_key: str
    action_seq: int

    model_config = ConfigDict(extra="forbid", frozen=True)

    def __str__(self) -> str:
        return f"{self.session_key}/{self.action_seq}"


# ── Action lifecycle states ───────────────────────────────────────────────────


class ActionStatus(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    DONE = "done"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class PendingState(BaseModel):
    """Awaiting operator decision."""

    status: Literal[ActionStatus.PENDING] = ActionStatus.PENDING

    model_config = ConfigDict(extra="forbid")


class ExecutingState(BaseModel):
    """Approved; backend call in flight."""

    status: Literal[ActionStatus.EXECUTING] = ActionStatus.EXECUTING

    model_config = ConfigDict(extra="forbid")


class DoneState(BaseModel):
    """Backend call completed. outcome.isError distinguishes success from tool error."""

    status: Literal[ActionStatus.DONE] = ActionStatus.DONE
    outcome: mcp_types.CallToolResult

    model_config = ConfigDict(extra="forbid")


class RejectedState(BaseModel):
    """Operator rejected the action."""

    status: Literal[ActionStatus.REJECTED] = ActionStatus.REJECTED
    reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class WithdrawnState(BaseModel):
    """Agent withdrew the action before it was decided."""

    status: Literal[ActionStatus.WITHDRAWN] = ActionStatus.WITHDRAWN

    model_config = ConfigDict(extra="forbid")


ActionState = Annotated[
    PendingState | ExecutingState | DoneState | RejectedState | WithdrawnState, Field(discriminator="status")
]


# ── Top-level action record ───────────────────────────────────────────────────


class Action(BaseModel):
    """One pending or resolved action record."""

    key: ActionKey
    created_at: datetime
    updated_at: datetime
    call: ToolCall
    justification: str
    state: ActionState
    client_id: str | None
    subject: str | None

    model_config = ConfigDict(extra="forbid")


# ── Append-only event log ───────────────────────────────────────────────────


class LogEventKind(StrEnum):
    ACTION_RECEIVED = "action_received"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_FINISHED = "execution_finished"


class ActionReceivedDetail(BaseModel):
    kind: Literal[LogEventKind.ACTION_RECEIVED] = LogEventKind.ACTION_RECEIVED
    model_config = ConfigDict(extra="forbid")


class DeniedDetail(BaseModel):
    kind: Literal[LogEventKind.DENIED] = LogEventKind.DENIED
    reason: str | None = None
    model_config = ConfigDict(extra="forbid")


class WithdrawnDetail(BaseModel):
    kind: Literal[LogEventKind.WITHDRAWN] = LogEventKind.WITHDRAWN
    model_config = ConfigDict(extra="forbid")


class ExecutionStartedDetail(BaseModel):
    kind: Literal[LogEventKind.EXECUTION_STARTED] = LogEventKind.EXECUTION_STARTED
    started_at: datetime
    model_config = ConfigDict(extra="forbid")


class ExecutionFinishedDetail(BaseModel):
    kind: Literal[LogEventKind.EXECUTION_FINISHED] = LogEventKind.EXECUTION_FINISHED
    outcome: mcp_types.CallToolResult
    model_config = ConfigDict(extra="forbid")


LogEventDetail = Annotated[
    ActionReceivedDetail | DeniedDetail | WithdrawnDetail | ExecutionStartedDetail | ExecutionFinishedDetail,
    Field(discriminator="kind"),
]


class LogEntry(BaseModel):
    """A single append-only event log entry, scoped to a session."""

    entry_id: int
    session_key: str
    action_seq: int
    detail: LogEventDetail
    timestamp: datetime

    model_config = ConfigDict(extra="forbid")


# ── Operator decisions ────────────────────────────────────────────────────────


class ApproveDecision(BaseModel):
    """Operator approved the action; gate will execute it against the backend."""

    kind: Literal["approved"] = "approved"

    model_config = ConfigDict(extra="forbid")


class DenyDecision(BaseModel):
    """Operator denied the action."""

    kind: Literal["denied"] = "denied"
    reason: str | None = None

    model_config = ConfigDict(extra="forbid")


OperatorDecision = Annotated[ApproveDecision | DenyDecision, Field(discriminator="kind")]


# ── Wait mode for tool calls ──────────────────────────────────────────────


class BlockingWait(BaseModel):
    """Wait indefinitely until terminal resolution."""

    mode: Literal["blocking"] = "blocking"

    model_config = ConfigDict(extra="forbid")


class YieldAfterMs(BaseModel):
    """Wait up to timeout_ms then return current state."""

    mode: Literal["yield_after_ms"] = "yield_after_ms"
    timeout_ms: float

    model_config = ConfigDict(extra="forbid")


WaitMode = Annotated[BlockingWait | YieldAfterMs, Field(discriminator="mode")]
