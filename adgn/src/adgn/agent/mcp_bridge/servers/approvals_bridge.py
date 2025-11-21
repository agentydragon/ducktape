"""Approvals server for agents bridge (exposes per-agent approval resources)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from adgn.agent.approvals import ApprovalHub
from adgn.agent.mcp_bridge.types import AgentID
from adgn.agent.persist import ApprovalOutcome, ToolCallRecord
from adgn.agent.types import ToolCall
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

if TYPE_CHECKING:
    from adgn.agent.persist import Persistence

logger = logging.getLogger(__name__)


class PendingApprovalItem(BaseModel):
    """A single pending approval."""
    call_id: str
    tool_call: ToolCall
    timestamp: datetime


class PendingApprovalsResponse(BaseModel):
    """Response containing pending approvals for an agent."""
    agent_id: AgentID
    pending: list[PendingApprovalItem]


class ApprovalHistoryItem(BaseModel):
    """A single approval history entry."""
    call_id: str
    tool_call: ToolCall
    outcome: ApprovalOutcome
    reason: str | None
    timestamp: datetime


class ApprovalHistoryResponse(BaseModel):
    """Response containing approval history for an agent."""
    agent_id: AgentID
    timeline: list[ApprovalHistoryItem]
    count: int


class ApprovalsBridgeServer(NotifyingFastMCP):
    """MCP server exposing approval resources for agents bridge.

    This server wraps ApprovalHub and provides resources at simplified paths
    that will be prefixed by the compositor to create the final hierarchical URIs.

    Resources (after mounting):
    - resource://agent{id}/approvals/pending - Pending approvals
    - resource://agent{id}/approvals/history - Approval history timeline
    """

    def __init__(self, approval_hub: ApprovalHub, persistence: Persistence, agent_id: AgentID):
        super().__init__(name=f"approvals_{agent_id}")
        self._hub = approval_hub
        self._persistence = persistence
        self._agent_id = agent_id
        self._register_resources()

    def _register_resources(self) -> None:
        @self.resource("resource://pending", name="pending", mime_type="application/json")
        async def get_pending() -> PendingApprovalsResponse:
            """Get pending approvals for this agent."""
            pending_map = self._hub.pending
            pending_list = [
                PendingApprovalItem(
                    call_id=call_id,
                    tool_call=tool_call,
                    timestamp=datetime.now(),  # Approx timestamp
                )
                for call_id, tool_call in pending_map.items()
            ]
            return PendingApprovalsResponse(
                agent_id=self._agent_id,
                pending=pending_list
            )

        @self.resource("resource://history", name="history", mime_type="application/json")
        async def get_history() -> ApprovalHistoryResponse:
            """Get approval history timeline for this agent."""
            # Get decided tool calls from persistence
            records = await self._persistence.get_tool_call_records(self._agent_id)

            # Convert to history entries (skip pending ones)
            timeline = []
            for record in records:
                if record.decision is not None:  # Only decided calls
                    timeline.append(
                        ApprovalHistoryItem(
                            call_id=record.tool_call.id,
                            tool_call=record.tool_call,
                            outcome=record.decision.outcome,
                            reason=record.decision.reason,
                            timestamp=record.decision.decided_at,
                        )
                    )

            return ApprovalHistoryResponse(
                agent_id=self._agent_id,
                timeline=timeline,
                count=len(timeline)
            )

    async def notify_approvals_changed(self) -> None:
        """Notify that approvals have changed (pending or history)."""
        await self.broadcast_resource_updated("resource://pending")
        await self.broadcast_resource_updated("resource://history")
