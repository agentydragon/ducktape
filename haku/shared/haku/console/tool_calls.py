"""The haku-console tool-call API contract.

The request/response models for haku-console's MCP tool-call approval endpoints
(`/api/tool-calls`, `/api/approvals/*`). Single source of truth for both repos: haku-console
(ducktape) serves these; haku-ui's backend (haku-state) parses them when it proxies operator
tool calls to the console. haku-ui layers its own `state_request_id`/`ToolRequestDoc` concept on
top of `SubmitToolCallRequest` — that stays in haku-state, since the console never sees it.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class SubmitToolCallRequest(BaseModel):
    server_id: str
    tool_name: str
    arguments: dict[str, Any]
    rationale: str = ""
    title: str | None = None
    wait_for_ms: int = Field(ge=0, le=60_000)


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    reason: str | None = None


class OperatorToolCallCaller(BaseModel):
    kind: Literal["operator"] = "operator"


class AgentToolCallCaller(BaseModel):
    kind: Literal["agent"] = "agent"
    agent_id: UUID
    display_name: str


type ToolCallCaller = Annotated[OperatorToolCallCaller | AgentToolCallCaller, Field(discriminator="kind")]


class ToolCallRecord(BaseModel):
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
    denial_reason: str | None = None
    approval_policy_id: str | None = None
    auto_approval_evaluation: str | None = None
    approved_at: datetime.datetime | None = None


class ToolCallEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_type: ToolCallEventType
    tool_call_id: str
    status: ToolCallStatus
    created_at: datetime.datetime
