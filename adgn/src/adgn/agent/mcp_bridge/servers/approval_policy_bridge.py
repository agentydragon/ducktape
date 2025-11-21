"""Approval policy server for agents bridge (exposes per-agent policy resources)."""

from __future__ import annotations

from datetime import datetime
import logging

from pydantic import BaseModel

from adgn.agent.approvals import ApprovalPolicyEngine
from adgn.agent.mcp_bridge.types import AgentID
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

logger = logging.getLogger(__name__)


class ProposalDescriptor(BaseModel):
    """Descriptor for a policy proposal."""
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None


class ProposalsList(BaseModel):
    """List of policy proposals for an agent."""
    agent_id: AgentID
    proposals: list[ProposalDescriptor]


class ProposalDetail(BaseModel):
    """Full details for a single policy proposal."""
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None
    content: str


class ApprovalPolicyBridgeServer(NotifyingFastMCP):
    """MCP server exposing approval policy resources for agents bridge.

    This server wraps ApprovalPolicyEngine and provides resources at simplified paths
    that will be prefixed by the compositor to create the final hierarchical URIs.

    Resources (after mounting):
    - resource://agent{id}/policy/policy.py - Active approval policy source code
    - resource://agent{id}/policy/proposals/list - List of policy proposals
    - resource://agent{id}/policy/proposals/{proposal_id} - Specific proposal details
    """

    def __init__(self, engine: ApprovalPolicyEngine, agent_id: AgentID):
        super().__init__(name=f"approval_policy_{agent_id}")
        self._engine = engine
        self._agent_id = agent_id
        self._register_resources()
        self._register_tools()

    def _register_resources(self) -> None:
        @self.resource("resource://policy.py", name="policy.py", mime_type="text/x-python")
        def active_policy() -> str:
            """Get the active approval policy source code."""
            source, _ = self._engine.get_policy()
            return source

        @self.resource("resource://proposals/list", name="proposals_list", mime_type="application/json")
        async def proposals_list() -> ProposalsList:
            """List all policy proposals with status and timestamps."""
            proposals = await self._engine.persistence.list_policy_proposals(self._engine.agent_id)
            return ProposalsList(
                agent_id=self._agent_id,
                proposals=[
                    ProposalDescriptor(
                        id=p.id,
                        status=ProposalStatus(p.status),
                        created_at=p.created_at,
                        decided_at=p.decided_at,
                    )
                    for p in proposals
                ]
            )

        @self.resource("resource://proposals/{id}", name="proposal_detail", mime_type="application/json")
        async def proposal_detail(id: str) -> ProposalDetail:
            """Get full proposal details including content and metadata."""
            got = await self._engine.persistence.get_policy_proposal(self._engine.agent_id, id)
            if got is None:
                raise KeyError(f"Proposal {id} not found")

            return ProposalDetail(
                id=got.id,
                status=ProposalStatus(got.status),
                created_at=got.created_at,
                decided_at=got.decided_at,
                content=got.content,
            )

    def _register_tools(self) -> None:
        @self.tool()
        async def set_policy(source: str) -> dict:
            """Update the active approval policy.

            Args:
                source: Python source code for the new policy

            Returns:
                Dictionary with the new policy_id
            """
            policy_id = await self._engine.set_policy(source)
            await self.notify_policy_changed()
            return {"policy_id": policy_id, "agent_id": self._agent_id}

        @self.tool()
        async def create_proposal(content: str) -> dict:
            """Create a new policy proposal.

            Args:
                content: Python source code for the proposed policy

            Returns:
                Dictionary with the new proposal_id
            """
            proposal_id = await self._engine.create_proposal(content)
            await self.notify_proposals_changed()
            return {"proposal_id": proposal_id, "agent_id": self._agent_id}

        @self.tool()
        async def approve_proposal(proposal_id: str) -> dict:
            """Approve and activate a policy proposal.

            Args:
                proposal_id: ID of the proposal to approve

            Returns:
                Dictionary confirming the approval
            """
            await self._engine.approve_proposal(int(proposal_id))
            await self.notify_policy_changed()
            await self.notify_proposals_changed()
            return {"status": "approved", "proposal_id": proposal_id, "agent_id": self._agent_id}

        @self.tool()
        async def reject_proposal(proposal_id: str) -> dict:
            """Reject a policy proposal.

            Args:
                proposal_id: ID of the proposal to reject

            Returns:
                Dictionary confirming the rejection
            """
            await self._engine.reject_proposal(int(proposal_id))
            await self.notify_proposals_changed()
            return {"status": "rejected", "proposal_id": proposal_id, "agent_id": self._agent_id}

    async def notify_policy_changed(self) -> None:
        """Notify that the policy has changed."""
        await self.broadcast_resource_updated("resource://policy.py")

    async def notify_proposals_changed(self) -> None:
        """Notify that the proposals list has changed."""
        await self.broadcast_resource_list_changed()
        await self.broadcast_resource_updated("resource://proposals/list")
