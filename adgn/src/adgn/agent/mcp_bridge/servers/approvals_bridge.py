"""Approvals server for agents bridge (exposes per-agent approval resources)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from adgn.agent.approvals import ApprovalHub
from adgn.agent.handler import ContinueDecision, DenyContinueDecision
from adgn.agent.mcp_bridge.servers.types import ApprovalStatus
from adgn.agent.mcp_bridge.types import AgentID
from adgn.agent.persist import ApprovalOutcome, ToolCallRecord
from adgn.agent.types import ToolCall
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

if TYPE_CHECKING:
    from adgn.agent.persist import Persistence

logger = logging.getLogger(__name__)


class ApprovalItem(BaseModel):
    """A single approval (pending or decided)."""
    call_id: str
    tool_call: ToolCall
    status: ApprovalStatus
    reason: str | None = None
    timestamp: datetime


class ApprovalsResponse(BaseModel):
    """Response containing all approvals for an agent (pending + decided history)."""
    agent_id: AgentID
    approvals: list[ApprovalItem]
    pending_count: int
    decided_count: int


class ApprovalsBridgeServer(NotifyingFastMCP):
    """MCP server exposing approval resources for agents bridge.

    This server wraps ApprovalHub and provides resources at simplified paths
    that will be prefixed by the compositor to create the final hierarchical URIs.

    Resources (after mounting):
    - resource://agent{id}/approvals/approvals - All approvals (pending + decided history)
    """

    def __init__(self, approval_hub: ApprovalHub, persistence: Persistence, agent_id: AgentID):
        super().__init__(name=f"approvals_{agent_id}")
        self._hub = approval_hub
        self._persistence = persistence
        self._agent_id = agent_id
        self._register_resources()
        self._register_tools()

    def _register_resources(self) -> None:
        @self.resource("resource://approvals", name="approvals", mime_type="application/json")
        async def get_approvals() -> ApprovalsResponse:
            """Get all approvals for this agent (pending + decided history)."""
            approvals_list = []
            pending_count = 0
            decided_count = 0

            # Add pending approvals
            pending_map = self._hub.pending
            for call_id, tool_call in pending_map.items():
                approvals_list.append(
                    ApprovalItem(
                        call_id=call_id,
                        tool_call=tool_call,
                        status=ApprovalStatus.PENDING,
                        reason=None,
                        timestamp=datetime.now(),  # Approx timestamp for pending
                    )
                )
                pending_count += 1

            # Add decided approvals from persistence
            records = await self._persistence.get_tool_call_records(self._agent_id)
            for record in records:
                if record.decision is not None:
                    # Map ApprovalOutcome to ApprovalStatus
                    if record.decision.outcome == ApprovalOutcome.APPROVED:
                        status = ApprovalStatus.APPROVED
                    elif record.decision.outcome == ApprovalOutcome.REJECTED:
                        status = ApprovalStatus.REJECTED
                    elif record.decision.outcome == ApprovalOutcome.DENIED:
                        status = ApprovalStatus.DENIED
                    elif record.decision.outcome == ApprovalOutcome.ABORTED:
                        status = ApprovalStatus.ABORTED
                    else:
                        # Fallback for unknown outcomes
                        status = ApprovalStatus.REJECTED

                    approvals_list.append(
                        ApprovalItem(
                            call_id=record.tool_call.id,
                            tool_call=record.tool_call,
                            status=status,
                            reason=record.decision.reason,
                            timestamp=record.decision.decided_at,
                        )
                    )
                    decided_count += 1

            # Sort by timestamp (most recent first)
            approvals_list.sort(key=lambda x: x.timestamp, reverse=True)

            return ApprovalsResponse(
                agent_id=self._agent_id,
                approvals=approvals_list,
                pending_count=pending_count,
                decided_count=decided_count,
            )

    def _register_tools(self) -> None:
        @self.tool()
        async def approve(call_id: str, reasoning: str | None = None) -> dict:
            """Approve a pending tool call.

            Args:
                call_id: ID of the tool call to approve
                reasoning: Optional reasoning for the approval

            Returns:
                Dictionary confirming the approval
            """
            decision = ContinueDecision(reasoning=reasoning)
            self._hub.resolve(call_id, decision)
            await self.notify_approvals_changed()
            return {"status": "approved", "call_id": call_id, "agent_id": self._agent_id}

        @self.tool()
        async def reject(call_id: str, reasoning: str | None = None) -> dict:
            """Reject a pending tool call.

            Args:
                call_id: ID of the tool call to reject
                reasoning: Optional reasoning for the rejection

            Returns:
                Dictionary confirming the rejection
            """
            decision = DenyContinueDecision(reason=reasoning or "Rejected by user")
            self._hub.resolve(call_id, decision)
            await self.notify_approvals_changed()
            return {"status": "rejected", "call_id": call_id, "agent_id": self._agent_id}

    async def notify_approvals_changed(self) -> None:
        """Notify that approvals have changed."""
        await self.broadcast_resource_updated("resource://approvals")
