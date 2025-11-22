from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from fastmcp.mcp_config import MCPConfig
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, JsonValue

from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.types import AgentID, ToolCall


class AgentRow(BaseModel):
    id: AgentID
    created_at: datetime
    mcp_config: MCPConfig
    preset: str
    model_config = ConfigDict(arbitrary_types_allowed=True)


class PersistenceRunStatus(StrEnum):
    """Final run state stored in persistence layer.

    Represents the terminal state of a run in the database.
    For transient UI states, see protocol.RunStatus.
    """
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"
    ABORTED = "aborted"


class ApprovalOutcome(StrEnum):
    POLICY_ALLOW = "policy_allow"
    POLICY_DENY_CONTINUE = "policy_deny_continue"
    POLICY_DENY_ABORT = "policy_deny_abort"
    USER_APPROVE = "user_approve"
    USER_DENY_CONTINUE = "user_deny_continue"
    USER_DENY_ABORT = "user_deny_abort"


class EventType(StrEnum):
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    REASONING = "reasoning"
    RESPONSE = "response"


class PolicyStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    PROPOSED = "proposed"
    REJECTED = "rejected"


class RunRow(BaseModel):
    id: UUID
    agent_id: AgentID
    started_at: datetime
    finished_at: datetime | None
    status: PersistenceRunStatus
    system_message: str | None
    model: str | None
    model_params: dict[str, JsonValue] | None
    model_config = ConfigDict(arbitrary_types_allowed=True)


class PolicyProposal(BaseModel):
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None
    content: str


class Decision(BaseModel):
    """Decision made about a tool call.

    All fields are REQUIRED. The entire Decision object is optional on ToolCallRecord.
    """

    outcome: ApprovalOutcome
    decided_at: datetime
    reason: str | None = None


class ToolCallExecution(BaseModel):
    """Tool execution result.

    All fields are REQUIRED. The entire ToolCallExecution object is optional on ToolCallRecord.
    """

    completed_at: datetime
    output: mcp_types.CallToolResult
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ToolCallRecord(BaseModel):
    """Complete tool call record from policy gate (tracks ALL calls through gate).

    States:
    - PENDING: decision=None, execution=None
    - EXECUTING: decision!=None, execution=None
    - COMPLETED: decision!=None, execution!=None
    """

    call_id: str
    run_id: str | None
    agent_id: AgentID  # REQUIRED - every tool call must be associated with an agent
    tool_call: ToolCall
    decision: Decision | None = None
    execution: ToolCallExecution | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


from .events import EventRecord, TypedPayload  # noqa: E402


class Persistence(Protocol):
    async def ensure_schema(self) -> None: ...

    # Agents API ---------------------------------------------------------------
    async def create_agent(self, *, mcp_config: MCPConfig, preset: str) -> AgentID: ...
    async def update_agent_specs(self, agent_id: AgentID, *, mcp_config: MCPConfig) -> None: ...
    async def patch_agent_specs(
        self, agent_id: AgentID, *, attach: dict[str, MCPConfig] = {}, detach: list[str] = []
    ) -> MCPConfig: ...
    async def list_agents(self) -> list[AgentRow]: ...
    async def get_agent(self, agent_id: AgentID) -> AgentRow | None: ...
    async def list_agents_last_activity(self) -> dict[AgentID, datetime | None]: ...
    async def delete_agent(self, agent_id: AgentID) -> None: ...

    # Runs API -----------------------------------------------------------------
    async def start_run(
        self,
        *,
        run_id: UUID,
        agent_id: AgentID,
        system_message: str | None,
        model: str | None,
        model_params: dict[str, JsonValue] | None,
        started_at: datetime,
    ) -> None: ...

    async def finish_run(self, run_id: UUID, *, status: PersistenceRunStatus, finished_at: datetime) -> None: ...

    async def append_event(
        self,
        *,
        run_id: UUID,
        seq: int,
        ts: datetime,
        type: EventType,
        payload: TypedPayload,
        call_id: str | None = None,
        tool_key: str | None = None,
    ) -> None: ...

    # ToolCallRecord API (new) -------------------------------------------------
    async def save_tool_call(self, record: ToolCallRecord) -> None:
        """Save or update a tool call record (INSERT OR REPLACE).

        Use this for all lifecycle stages:
        - PENDING: decision=None, execution=None
        - EXECUTING: decision!=None, execution=None
        - COMPLETED: decision!=None, execution!=None
        """
        ...

    async def get_tool_call(self, call_id: str) -> ToolCallRecord | None:
        """Get a tool call record by call_id."""
        ...

    async def list_tool_calls(self, run_id: str | None = None) -> list[ToolCallRecord]:
        """List tool call records, optionally filtered by run_id."""
        ...

    async def list_runs(self, *, agent_id: AgentID | None = None, limit: int = 50) -> list[RunRow]: ...
    async def get_run(self, run_id: UUID) -> RunRow | None: ...
    async def load_events(self, run_id: UUID) -> list[EventRecord]: ...

    # Approval policy (per-agent) --------------------------------------------
    async def get_latest_policy(self, agent_id: AgentID) -> tuple[str, int] | None: ...
    async def set_policy(self, agent_id: AgentID, *, content: str) -> int: ...

    # Approval policy proposals (single store impl: SQLite)
    async def create_policy_proposal(self, agent_id: AgentID, *, proposal_id: int, content: str) -> int: ...
    async def list_policy_proposals(self, agent_id: AgentID) -> list[PolicyProposal]: ...
    async def get_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> PolicyProposal | None: ...
    async def approve_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> int: ...
    async def reject_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> None: ...
