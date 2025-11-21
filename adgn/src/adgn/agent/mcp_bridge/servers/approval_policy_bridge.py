"""Approval policy server for agents bridge (exposes per-agent policy resources)."""

from __future__ import annotations

import json
import logging

from adgn.agent.approvals import ApprovalPolicyEngine
from adgn.agent.mcp_bridge.types import AgentID
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

logger = logging.getLogger(__name__)


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

    def _register_resources(self) -> None:
        @self.resource("resource://policy.py", name="policy.py", mime_type="text/x-python")
        def active_policy() -> str:
            """Get the active approval policy source code."""
            source, _ = self._engine.get_policy()
            return source

        @self.resource("resource://proposals/list", name="proposals_list", mime_type="application/json")
        async def proposals_list() -> str:
            """List all policy proposals with status and timestamps."""
            proposals = await self._engine.persistence.list_policy_proposals(self._engine.agent_id)
            return json.dumps({
                "agent_id": self._agent_id,
                "proposals": [
                    {
                        "id": p.id,
                        "status": ProposalStatus(p.status).value,
                        "created_at": p.created_at.isoformat(),
                        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
                    }
                    for p in proposals
                ]
            })

        @self.resource("resource://proposals/{id}", name="proposal_detail", mime_type="application/json")
        async def proposal_detail(id: str) -> str:
            """Get full proposal details including content and metadata."""
            got = await self._engine.persistence.get_policy_proposal(self._engine.agent_id, id)
            if got is None:
                raise KeyError(f"Proposal {id} not found")

            return json.dumps({
                "id": got.id,
                "status": ProposalStatus(got.status).value,
                "created_at": got.created_at.isoformat(),
                "decided_at": got.decided_at.isoformat() if got.decided_at else None,
                "content": got.content,
            })

    async def notify_policy_changed(self) -> None:
        """Notify that the policy has changed."""
        await self.broadcast_resource_updated("resource://policy.py")

    async def notify_proposals_changed(self) -> None:
        """Notify that the proposals list has changed."""
        await self.broadcast_resource_list_changed()
        await self.broadcast_resource_updated("resource://proposals/list")
