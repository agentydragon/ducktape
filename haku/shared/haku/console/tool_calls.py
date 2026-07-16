"""The haku-console tool-call wire contract.

Single source of truth for the records haku-console exposes to its operator UI and the metadata
its agent-facing MCP server advertises. Haku-state consumes the MCP metadata to resolve an authored
``(server_id, tool_name)`` request to the exact reflected MCP tool without duplicating name-mangling
rules.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

MCP_TOOL_META_KEY = "works.allegedly.haku/tool"
MCP_TOOL_CALL_META_KEY = "works.allegedly.haku/tool-call"


class McpProxyToolMetadata(BaseModel):
    server_id: str
    upstream_tool_name: str
    approval_mode: Literal["passthrough", "approval_required"]


class McpToolCallMetadata(BaseModel):
    tool_call_id: str


class ToolCallStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


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
