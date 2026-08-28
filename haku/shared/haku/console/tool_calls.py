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


# Conflates "which input-schema shape does the proxy tool advertise" (enveloped vs raw)
# with "does a call auto-approve" — roughly 1-1 today, a coupling the interface should not
# encode. Split tracked in haku/console/TODO.md § Small cleanups.
class ApprovalMode(StrEnum):
    PASSTHROUGH = "passthrough"
    APPROVAL_REQUIRED = "approval_required"


class McpProxyToolMetadata(BaseModel):
    server_id: str
    upstream_tool_name: str
    approval_mode: ApprovalMode


class McpToolCallMetadata(BaseModel):
    tool_call_id: str


class ToolCallStatus(StrEnum):
    """Append only: the string values are parsed off the wire by haku-state, and the Postgres
    ``tool_call_status`` enum's label order must match the declaration order (live-database
    relation: test_mcp_approval.py::test_fresh_baseline_enum_values_match_domain_enums).
    """

    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    # Terminal: the submitting Agent retracted the request before an operator decided it.
    WITHDRAWN = "withdrawn"


class ToolCallPayloadField(StrEnum):
    ARGUMENTS = "arguments"
    CALLER = "caller"
    RATIONALE = "rationale"
    RESULT = "result"


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
    session_id: UUID | None = None


type ToolCallCaller = Annotated[OperatorToolCallCaller | AgentToolCallCaller, Field(discriminator="kind")]


class ToolCallRecord(BaseModel):
    """The single actor-scoped domain record behind console and MCP edge projections."""

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
    withdrawal_reason: str | None = None
    approval_policy_id: str | None = None
    auto_approval_evaluation: str | None = None
    approved_at: datetime.datetime | None = None
