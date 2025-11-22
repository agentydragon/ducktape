from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib import resources
import logging
from typing import TYPE_CHECKING
import uuid

from docker import DockerClient
from pydantic import BaseModel

from adgn.agent.handler import AbortTurnDecision, ContinueDecision, DenyContinueDecision
from adgn.agent.models.policy_error import PolicyError
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.persist import Persistence, PolicyProposal
from adgn.agent.policy_eval.runner import run_policy_source
from adgn.agent.types import AgentID, ApprovalStatus, ToolCall
from adgn.mcp._shared.constants import (
    AGENTS_POLICY_STATE_URI_FMT,
    APPROVAL_POLICY_PROPOSALS_INDEX_URI,
    APPROVAL_POLICY_RESOURCE_URI,
    UI_SERVER_NAME,
)
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

# build_mcp_function is used for self_check payload construction

logger = logging.getLogger(__name__)


class PolicyValidationError(Exception):
    def __init__(self, message: str, details: PolicyError | None = None) -> None:
        super().__init__(message)
        self.details: PolicyError | None = details


# Policy source is executed only inside the container evaluator


# TODO(approval-policy follow-ups)
# - Resource operations are exposed as MCP tools (e.g., resources_list,
#   resources_read) and are gated by the Policy Gateway middleware.
#   The default policy allows RESOURCES server ops; tighten policy as needed.
# - Policy sandboxing: Execute user policy code under a stricter sandbox. Today
#   we execute with standard Python builtins and require explicit imports; future
#   hardening may restrict imports or isolate execution.
# - Persistence/versioning UX: Persistence exists (SQLite) for policy IDs and
#   proposals, but richer history/metadata and rollback tools could improve UX.
# - Multi-user/editor UX: Add conflict prevention and richer notifications for
#   concurrent edits/approvals (e.g., optimistic locking, better UI affordances).


class TurnAbortRequested(Exception):  # noqa: N818
    # TODO: Reconsider whether signalling turn abort via exceptions is the best approach
    def __init__(self, call_id: str, reason: str = "approval_denied", context: dict | None = None) -> None:
        self.call_id = call_id
        self.reason = reason
        self.context = context or {}
        super().__init__(f"Turn abort requested: {reason} (call_id={call_id})")


class ApprovalItem(BaseModel):
    """A single approval (pending or decided)."""
    call_id: str
    tool_call: ToolCall
    status: ApprovalStatus
    reason: str | None = None
    updated_at: datetime


class ApprovalsResponse(BaseModel):
    """Response containing all approvals for an agent (pending + decided history)."""
    agent_id: AgentID
    approvals: list[ApprovalItem]


@dataclass
class PendingApproval:
    """Pending approval with tool call and future."""

    tool_call: ToolCall
    future: asyncio.Future[ContinueDecision | DenyContinueDecision | AbortTurnDecision]


class ApprovalHub(NotifyingFastMCP):
    """In-process rendezvous for pending approval/decision events with MCP server.

    - await_decision(call_id, request) -> Decision waits until resolve() is called
    - resolve(call_id, decision) resolves the pending decision

    MCP Resources (when agent_id and persistence are provided):
    - resource://approvals - All approvals (pending + decided history)

    MCP Tools (when agent_id and persistence are provided):
    - approve(call_id, reasoning) - Approve a pending tool call
    - reject(call_id, reasoning) - Reject a pending tool call
    """

    def __init__(self, agent_id: AgentID | None = None, persistence: Persistence | None = None) -> None:
        super().__init__(name=f"approvals_{agent_id}" if agent_id else "approvals_test")
        self._agent_id = agent_id
        self._persistence = persistence
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._has_mcp = agent_id is not None and persistence is not None
        # Only register MCP resources/tools if agent_id and persistence are provided
        if self._has_mcp:
            self._register_resources()
            self._register_tools()

    async def await_decision(
        self, call_id: str, tool_call: ToolCall
    ) -> ContinueDecision | DenyContinueDecision | AbortTurnDecision:
        async with self._lock:
            pending = self._pending.get(call_id)
            if pending is None:
                fut = asyncio.get_running_loop().create_future()
                self._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)
            else:
                fut = pending.future
        if self._has_mcp:
            await self.notify_approvals_changed()
        return await fut

    @property
    def pending(self) -> dict[str, ToolCall]:
        """Public view of pending approval tool calls (immutable contract by convention)."""
        return {call_id: p.tool_call for call_id, p in self._pending.items()}

    def _register_resources(self) -> None:
        @self.resource("resource://approvals", name="approvals", mime_type="application/json")
        async def get_approvals() -> ApprovalsResponse:
            """Get all approvals for this agent (pending + decided history)."""
            # Build pending approvals
            pending_approvals = [
                ApprovalItem(
                    call_id=call_id,
                    tool_call=tool_call,
                    status=ApprovalStatus.PENDING,
                    reason=None,
                    updated_at=datetime.now(),  # Current time for pending
                )
                for call_id, tool_call in self.pending.items()
            ]

            # Build decided approvals from persistence
            records = await self._persistence.get_tool_call_records(self._agent_id)

            decided_approvals = [
                ApprovalItem(
                    call_id=record.tool_call.id,
                    tool_call=record.tool_call,
                    status=record.decision.outcome,  # Already ApprovalStatus after unification
                    reason=record.decision.reason,
                    updated_at=record.decision.decided_at,
                )
                for record in records
                if record.decision is not None
            ]

            # Combine and sort by updated_at (most recent first)
            return ApprovalsResponse(
                agent_id=self._agent_id,
                approvals=sorted(
                    pending_approvals + decided_approvals,
                    key=lambda x: x.updated_at,
                    reverse=True,
                ),
            )

    def _register_tools(self) -> None:
        @self.tool()
        async def approve(call_id: str, reasoning: str | None = None) -> dict:
            """Approve a pending tool call.

            Returns:
                Dictionary confirming the approval
            """
            # Inline resolve logic
            pending = self._pending.pop(call_id, None)
            if pending is not None and not pending.future.done():
                pending.future.set_result(ContinueDecision(reasoning=reasoning))
            await self.notify_approvals_changed()
            return {"status": "approved", "call_id": call_id, "agent_id": self._agent_id}

        @self.tool()
        async def reject(call_id: str, reasoning: str | None = None) -> dict:
            """Reject a pending tool call.

            Returns:
                Dictionary confirming the rejection
            """
            # Inline resolve logic
            pending = self._pending.pop(call_id, None)
            if pending is not None and not pending.future.done():
                pending.future.set_result(DenyContinueDecision(reason=reasoning or "Rejected by user"))
            await self.notify_approvals_changed()
            return {"status": "rejected", "call_id": call_id, "agent_id": self._agent_id}

    async def notify_approvals_changed(self) -> None:
        """Notify that approvals have changed."""
        await self.broadcast_resource_updated("resource://approvals")


# ---- Approval Policy Engine (decoupled, in-memory; optional) ----


class WellKnownTools(StrEnum):
    SEND_MESSAGE = "send_message"
    END_TURN = "end_turn"
    SANDBOX_EXEC = "sandbox_exec"  # adgn.mcp.seatbelt_exec.server


def load_default_policy_source() -> str:
    """Load the packaged default approval policy source code as text."""
    return resources.files("adgn.agent.policies").joinpath("default_policy.py").read_text(encoding="utf-8")


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


class ApprovalPolicyEngine(NotifyingFastMCP):
    """Single source of truth for active policy text/version with MCP server.

    Validation and execution are delegated to the Docker-backed evaluator.

    MCP Resources:
    - resource://policy.py - Active approval policy source code
    - resource://proposals/list - List of policy proposals
    - resource://proposals/{id} - Specific proposal details

    MCP Tools:
    - set_policy(source) - Update the active approval policy
    - create_proposal(content) - Create a new policy proposal
    - approve_proposal(proposal_id) - Approve and activate a policy proposal
    - reject_proposal(proposal_id) - Reject a policy proposal
    """

    def __init__(
        self,
        *,
        docker_client: DockerClient,
        agent_id: AgentID,
        persistence: Persistence,
        policy_source: str,
    ) -> None:
        super().__init__(name=f"approval_policy_{agent_id}")
        # DI of initial policy source; caller must pass explicit policy text.
        self._policy_source: str = policy_source
        # In-memory version counter for MCP resource change notifications.
        # Independent of SQL primary key (which is auto-incrementing per agent).
        # This tracks resource versions for the MCP protocol; SQL ID is for persistence.
        self._policy_id: int = 1  # Start at 1 since we have default content
        # Public attributes for engine wiring; keep simple access patterns
        self.docker_client: DockerClient = docker_client
        self.agent_id: AgentID = agent_id
        self.persistence: Persistence = persistence
        self._register_resources()
        self._register_tools()

    def get_policy(self) -> tuple[str, int]:
        return self._policy_source, self._policy_id

    async def set_policy(self, source: str) -> int:
        """Store new policy and return its database ID.

        Note: This method is called from both the set_policy tool and approve_proposal tool,
        so it is not inlined despite being primarily tool-facing.
        """
        self._policy_source = source
        record = await self.persistence.set_policy(self.agent_id, content=source)
        self._policy_id = record.id
        await self.notify_policy_changed()
        return self._policy_id

    # Internal load used on startup to hydrate content/id from persistence
    def load_policy(self, source: str, *, policy_id: int) -> None:
        # Hydrate from persistence without executing the code
        self._policy_source = source
        self._policy_id = policy_id

    def self_check(self, source: str) -> None:
        if self.docker_client is None:
            return  # Skip validation if Docker not available
        run_policy_source(
            docker_client=self.docker_client,
            source=source,
            input_payload={"name": build_mcp_function(UI_SERVER_NAME, "send_message"), "arguments": {}},
        )

    async def _get_proposal_or_raise(self, proposal_id: int | str) -> PolicyProposal:
        """Get policy proposal by ID or raise KeyError if not found."""
        got = await self.persistence.get_policy_proposal(self.agent_id, proposal_id)
        if got is None:
            raise KeyError(f"Proposal {proposal_id} not found")
        return got

    def _register_resources(self) -> None:
        @self.resource("resource://policy.py", name="policy.py", mime_type="text/x-python")
        def active_policy() -> str:
            """Get the active approval policy source code."""
            source, _ = self.get_policy()
            return source

        @self.resource("resource://proposals/list", name="proposals_list", mime_type="application/json")
        async def proposals_list() -> ProposalsList:
            """List all policy proposals with status and timestamps."""
            return ProposalsList(
                agent_id=self.agent_id,
                proposals=[
                    ProposalDescriptor(
                        id=p.id,
                        status=p.status,
                        created_at=p.created_at,
                        decided_at=p.decided_at,
                    )
                    for p in await self.persistence.list_policy_proposals(self.agent_id)
                ]
            )

        @self.resource("resource://proposals/{id}", name="proposal_detail", mime_type="application/json")
        async def proposal_detail(id: str) -> ProposalDetail:
            """Get full proposal details including content and metadata."""
            got = await self._get_proposal_or_raise(id)
            return ProposalDetail(
                id=got.id,
                status=got.status,
                created_at=got.created_at,
                decided_at=got.decided_at,
                content=got.content,
            )

    def _register_tools(self) -> None:
        @self.tool()
        async def set_policy(source: str) -> dict:
            """Update the active approval policy.

            Returns:
                Dictionary with the new policy_id
            """
            policy_id = await self.set_policy(source)
            await self.notify_policy_changed()
            return {"policy_id": policy_id, "agent_id": self.agent_id}

        @self.tool()
        async def create_proposal(content: str) -> dict:
            """Create a new policy proposal.

            Returns:
                Dictionary with the new proposal_id
            """
            # Inline create_proposal logic
            self.self_check(content)
            proposal_id = await self.persistence.create_policy_proposal(self.agent_id, proposal_id=0, content=content)
            await self.notify_proposal_change(proposal_id)
            return {"proposal_id": proposal_id, "agent_id": self.agent_id}

        @self.tool()
        async def approve_proposal(proposal_id: str) -> dict:
            """Approve and activate a policy proposal.

            Returns:
                Dictionary confirming the approval
            """
            # Inline approve_proposal logic
            got = await self._get_proposal_or_raise(int(proposal_id))
            self.self_check(got.content)
            # Activate policy (notifies via engine's set_policy)
            await self.set_policy(got.content)
            await self.persistence.approve_policy_proposal(self.agent_id, int(proposal_id))
            await self.notify_proposal_change(int(proposal_id))
            return {"status": "approved", "proposal_id": proposal_id, "agent_id": self.agent_id}

        @self.tool()
        async def reject_proposal(proposal_id: str) -> dict:
            """Reject a policy proposal.

            Returns:
                Dictionary confirming the rejection
            """
            # Inline reject_proposal logic
            await self.persistence.reject_policy_proposal(self.agent_id, int(proposal_id))
            await self.notify_proposal_change(int(proposal_id))
            return {"status": "rejected", "proposal_id": proposal_id, "agent_id": self.agent_id}

    async def notify_policy_changed(self) -> None:
        """Notify that the policy has changed."""
        await self.broadcast_resource_updated("resource://policy.py")
        await self.broadcast_resource_updated(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))

    async def notify_proposals_changed(self) -> None:
        """Notify that the proposals list has changed."""
        await self.broadcast_resource_list_changed()
        await self.broadcast_resource_updated("resource://proposals/list")

    async def notify_proposal_change(self, proposal_id: int) -> None:
        """Notify about a specific proposal change and the proposals index."""
        await self.broadcast_resource_updated(f"resource://proposals/{proposal_id}")
        await self.notify_proposals_changed()
        await self.broadcast_resource_updated(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))


def make_policy_engine(
    *,
    agent_id: AgentID,
    persistence: Persistence,
    docker_client: DockerClient,
    policy_source: str,
) -> ApprovalPolicyEngine:
    """Factory for ApprovalPolicyEngine with required context.

    Centralizes creation for wiring, CLI, and tests without hiding parameters.
    """
    return ApprovalPolicyEngine(
        docker_client=docker_client, agent_id=agent_id, persistence=persistence, policy_source=policy_source
    )

    # No set_context: engine must be constructed with required context

    # No in-engine tests; proposals/policies are validated by executing in Docker

    # No in-process decide helpers

    # No proposal APIs here; proposals handled by approval policy server/persistence

    # No seatbelt resolution or policy fields here; keep context transport-agnostic

    # Default repr is sufficient; no custom string/repr implementation


# No agent-level approval handler: Policy Gateway middleware enforces approvals.
