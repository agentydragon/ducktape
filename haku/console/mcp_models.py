"""Shared models for haku-console MCP approval calls."""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


class ToolCallEventType(StrEnum):
    TOOL_CALL_SUBMITTED = "tool_call_submitted"
    APPROVAL_PENDING = "approval_pending"
    TOOL_CALL_UPDATED = "tool_call_updated"


class SubmitToolCallRequest(BaseModel):
    server_id: str
    tool_name: str
    arguments: dict[str, Any]
    rationale: str = ""
    title: str | None = None
    wait_for_ms: int = Field(ge=0, le=60_000)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str | None = None


class ToolCallRecord(BaseModel):
    tool_call_id: str
    server_id: str
    tool_name: str
    caller_principal: str
    status: ToolCallStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime
    arguments: dict[str, Any]
    rationale: str = ""
    title: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    denial_reason: str | None = None


class ToolCallEvent(BaseModel):
    event_id: int
    event_type: ToolCallEventType
    tool_call_id: str
    status: ToolCallStatus
    created_at: datetime.datetime
