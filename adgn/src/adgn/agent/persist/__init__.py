from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from fastmcp.mcp_config import MCPConfig
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, JsonValue

from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.types import AgentID, ApprovalStatus, ToolCall


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

    outcome: ApprovalStatus
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


@dataclass
class PolicyRecord:
    """Active policy record."""

    id: int
    content: str
    created_at: datetime
    agent_id: AgentID


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
        """Save or update a tool call record (INSERT OR REPLACE)."""
        ...

    async def get_tool_call(self, call_id: str) -> ToolCallRecord | None: ...

    async def list_tool_calls(self, run_id: str | None = None) -> list[ToolCallRecord]:
        """List tool call records, optionally filtered by run_id."""
        ...

    async def list_runs(self, *, agent_id: AgentID | None = None, limit: int = 50) -> list[RunRow]: ...
    async def get_run(self, run_id: UUID) -> RunRow | None: ...
    async def load_events(self, run_id: UUID) -> list[EventRecord]: ...

    # Approval policy (per-agent) --------------------------------------------
    async def get_latest_policy(self, agent_id: AgentID) -> PolicyRecord | None:
        """Get latest active policy, or None if no policy set."""
        ...

    async def set_policy(self, agent_id: AgentID, *, content: str) -> PolicyRecord:
        """Set new policy and return the created record."""
        ...

    # Approval policy proposals (single store impl: SQLite)
    async def create_policy_proposal(self, agent_id: AgentID, *, proposal_id: int, content: str) -> int: ...
    async def list_policy_proposals(self, agent_id: AgentID) -> list[PolicyProposal]: ...
    async def get_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> PolicyProposal | None: ...
    async def approve_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> int: ...
    async def reject_policy_proposal(self, agent_id: AgentID, proposal_id: int) -> None: ...
