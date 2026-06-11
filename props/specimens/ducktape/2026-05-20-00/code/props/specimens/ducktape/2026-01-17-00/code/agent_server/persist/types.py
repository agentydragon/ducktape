from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from fastmcp.mcp_config import MCPConfig
from pydantic import BaseModel, ConfigDict, JsonValue

from agent_core.events import EventType as Event
from agent_server.agent_types import AgentID
from agent_server.models.proposal_status import ProposalStatus


class AgentMetadata(BaseModel):
    """Typed per-agent metadata stored in persistence.

    Currently only preset name is tracked; expand here if new metadata is added.
    """

    preset: str


class AgentRow(BaseModel):
    id: AgentID
    created_at: datetime
    mcp_config: MCPConfig
    metadata: AgentMetadata
    model_config = ConfigDict(arbitrary_types_allowed=True)


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


class PolicyProposal(BaseModel):
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None
    content: str


class Persistence(Protocol):
    async def ensure_schema(self) -> None: ...

    # Agents API ---------------------------------------------------------------
    async def create_agent(self, *, mcp_config: MCPConfig, metadata: AgentMetadata) -> AgentID: ...
    async def update_agent_specs(self, agent_id: AgentID, *, mcp_config: MCPConfig) -> None: ...
    async def list_agents(self) -> list[AgentRow]: ...
    async def get_agent(self, agent_id: AgentID) -> AgentRow | None: ...
    async def list_agents_last_activity(self) -> dict[AgentID, datetime | None]: ...
    async def delete_agent(self, agent_id: AgentID) -> None: ...

    # Events and approvals -----------------------------------------------------
    async def append_event(self, *, agent_id: AgentID, seq: int, ts: datetime, event: Event) -> None: ...

    async def record_approval(
        self,
        *,
        agent_id: AgentID,
        call_id: str,
        tool_key: str,
        outcome: ApprovalOutcome,
        decided_at: datetime,
        details: dict[str, JsonValue] | None = None,
    ) -> None: ...

    # Approval policy (per-agent) --------------------------------------------
    async def get_latest_policy(self, agent_id: AgentID) -> tuple[str, int] | None: ...
    async def set_policy(self, agent_id: AgentID, *, content: str) -> int: ...

    # Approval policy proposals (single store impl: SQLite)
    async def create_policy_proposal(self, agent_id: AgentID, *, proposal_id: str, content: str) -> None: ...
    async def list_policy_proposals(self, agent_id: AgentID) -> list[PolicyProposal]: ...
    async def get_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> PolicyProposal | None: ...
    async def approve_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> int: ...
    async def reject_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> None: ...
    async def delete_policy_proposal(self, agent_id: AgentID, proposal_id: str) -> None: ...
