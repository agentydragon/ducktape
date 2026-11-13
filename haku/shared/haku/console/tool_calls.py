"""Shared tool-call types for the database status schema and retained history renderers."""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

MAX_DECISION_NOTE_LENGTH = 4096


def _normalize_decision_note(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class ToolCallStatus(StrEnum):
    """Append only: PostgreSQL ``tool_call_status`` labels follow declaration order.

    The live-database relation is
    ``haku/console/test_agent_authority_schema.py::test_fresh_baseline_enum_values_match_domain_enums``.
    """

    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    # Terminal: the submitting Agent retracted the request before an operator decided it.
    WITHDRAWN = "withdrawn"


class OperatorToolCallCaller(BaseModel):
    kind: Literal["operator"] = "operator"


class AgentToolCallCaller(BaseModel):
    kind: Literal["agent"] = "agent"
    agent_id: UUID
    display_name: str
    session_id: UUID | None = None


type ToolCallCaller = Annotated[OperatorToolCallCaller | AgentToolCallCaller, Field(discriminator="kind")]


class ToolCallRecord(BaseModel):
    """The stored tool-call record used by the retained history renderers."""

    tool_call_id: str
    server_id: str
    tool_name: str
    caller: ToolCallCaller
    status: ToolCallStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime
    arguments: dict[str, Any]
    rationale: str = ""
    title: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    decision_note: str | None = Field(default=None, max_length=MAX_DECISION_NOTE_LENGTH)
    decision_operator_id: UUID | None = None
    withdrawal_reason: str | None = None
    approval_policy_id: str | None = None
    auto_approval_evaluation: str | None = None
    approved_at: datetime.datetime | None = None

    @field_validator("decision_note", mode="before")
    @classmethod
    def normalize_note(cls, value: Any) -> Any:
        return _normalize_decision_note(value)
