"""Unified cross-agent management MCP server.

Provides resources and tools for managing multiple agents from a single connection.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
import json
import logging
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp.mcp_config import MCPConfig, MCPServerTypes
from mcp import types as mcp_types
from pydantic import BaseModel, Field, TypeAdapter

from adgn.agent.handler import AbortTurnDecision, ContinueDecision, DenyContinueDecision
from adgn.agent.mcp_bridge import resources
from adgn.agent.mcp_bridge.types import AgentID, AgentMode
from adgn.agent.types import ToolCall
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.persist import ApprovalOutcome, ToolCallRecord
from adgn.agent.policies.policy_types import UserApprovalDecision
from adgn.agent.presets import discover_presets
from adgn.mcp._shared.types import SimpleOk
from adgn.mcp.approval_policy.server import ApproveProposalArgs, RejectProposalArgs, SetPolicyTextArgs
from adgn.mcp.compositor.server import MountEvent
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP


class AgentBrief(BaseModel):
    """Brief information about an agent (returned from create_agent tool)."""

    id: AgentID = Field(description="Unique agent identifier")


if TYPE_CHECKING:
    from adgn.agent.mcp_bridge.server import InfrastructureRegistry

logger = logging.getLogger(__name__)


# Helper functions for data conversion


def _convert_pending_approvals(pending_map: dict[str, ToolCall]) -> list[PendingApproval]:
    result: list[PendingApproval] = []
    for _call_id, tool_call in pending_map.items():
        result.append(
            PendingApproval(
                tool_call=tool_call,
                timestamp=datetime.now(),  # TODO: Track creation time in PendingApproval or separately
            )
        )
    return result


def _convert_tool_call_record_to_history(record: ToolCallRecord) -> ApprovalHistoryEntry | None:
    """Convert ToolCallRecord to ApprovalHistoryEntry.

    Returns None for PENDING tool calls (decision=None), since they haven't been decided yet
    and belong in the pending list instead.
    """
    # Skip pending tool calls - they go in the pending list, not history
    if record.decision is None:
        return None

    return ApprovalHistoryEntry(
        tool_call=record.tool_call,
        outcome=record.decision.outcome,
        reason=record.decision.reason,
        timestamp=record.decision.decided_at,
    )


# Enumerations
# Tool input models
class ApproveToolCallArgs(BaseModel):
    """Arguments for approve_tool_call tool."""

    agent_id: AgentID
    call_id: str


class RejectToolCallArgs(BaseModel):
    """Arguments for reject_tool_call tool."""

    agent_id: AgentID
    call_id: str
    reason: str


class DenyToolCallArgs(BaseModel):
    """Arguments for deny_tool_call tool (semantic alias for reject_tool_call)."""

    agent_id: AgentID
    call_id: str
    reason: str


class DenyAbortArgs(BaseModel):
    """Arguments for deny_abort tool."""

    agent_id: AgentID
    call_id: str
    reason: str


class AbortAgentArgs(BaseModel):
    """Arguments for abort_agent tool."""

    agent_id: AgentID


# Pending approval models
class PendingApproval(BaseModel):
    """A tool call awaiting approval."""

    tool_call: ToolCall
    timestamp: datetime


# Historical approval timeline models
class ApprovalHistoryEntry(BaseModel):
    """Single approval decision in the timeline."""

    tool_call: ToolCall
    outcome: ApprovalOutcome
    reason: str | None = None
    timestamp: datetime


# Resource response models
class AgentInfo(BaseModel):
    """Information about a single agent."""

    agent_id: AgentID
    capabilities: dict[str, bool]  # e.g., {"chat": True, "agent_loop": False}
    mode: AgentMode
    state_uri: str | None = None
    approvals_uri: str | None = None
    policy_proposals_uri: str | None = None


class AgentList(BaseModel):
    """Content for resource://agents/list."""

    agents: list[AgentInfo]


class AgentApprovalsPending(BaseModel):
    """Content for resource://agents/{id}/approvals/pending."""

    agent_id: AgentID
    pending: list[PendingApproval]


class AgentApprovalsHistory(BaseModel):
    """Content for resource://agents/{id}/approvals/history."""

    agent_id: AgentID
    timeline: list[ApprovalHistoryEntry]
    pending: list[PendingApproval]  # Pending approvals not yet decided
    count: int  # Total count (timeline + pending)


class PolicyProposalInfo(BaseModel):
    """Policy proposal metadata with URI to full content."""

    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None
    proposal_uri: str  # URI to access full proposal content in policy server


class AgentPolicyProposals(BaseModel):
    """Content for resource://agents/{id}/policy/proposals."""

    agent_id: AgentID
    proposals: list[PolicyProposalInfo]
    active_policy_uri: str  # URI to active policy


class AgentPolicyState(BaseModel):
    """Content for resource://agents/{id}/policy/state."""

    agent_id: AgentID = Field(description="Target agent identifier")
    policy: dict[str, Any] = Field(description="Policy state containing: content (Python source), id (policy identifier), proposals (list)")
    active_policy_uri: str = Field(description="URI to active policy resource")


class ServerStatus(StrEnum):
    """Agent server runtime status."""

    RUNNING = "running"
    STOPPED = "stopped"


class AgentInfoDetailed(BaseModel):
    """Basic agent metadata NOT available from other MCP resources.

    For additional data, query the specific MCP resources:
    - Compositor state: resource://agents/{id}/snapshot
    - Policy: resource://approval-policy/policy.py (per-agent server)
    - Approvals: resource://agents/{id}/approvals/pending, resource://agents/{id}/approvals/history
    """

    agent_id: AgentID
    mode: AgentMode
    model: str | None = None  # Model name for local agents
    status: ServerStatus


class PresetSummary(BaseModel):
    """Summary information about an agent preset."""

    name: str
    description: str | None = None


class PresetsList(BaseModel):
    """Content for resource://presets/list."""

    presets: list[PresetSummary]


# Tool response models - empty responses, caller tracks what they called


async def make_agents_server(registry: InfrastructureRegistry) -> NotifyingFastMCP:
    """Unified cross-agent management server.

    Can be delegated to other agents for self-orchestration.
    """
    server = NotifyingFastMCP(
        name="agents",
        instructions="""Multi-agent management server.

        Provides cross-agent visibility and control:
        - List all agents with their capabilities
        - View sampling state for local agents
        - Approve/reject tool calls
        - Abort running agents

        Future: spawn agents, update policies, delegate work.""",
    )

    # Resources

    @server.resource(
        "resource://agents/list",
        name="agents.list",
        mime_type="application/json",
        description="List all agents with detailed status",
    )
    async def list_agents() -> str:
        """Global agent list with detailed status for each agent.

        Returns JSON with agents array containing status information including:
        - id, mode, live status
        - active_run_id, run_phase
        - pending_approvals count
        - capabilities (chat, agent_loop)
        """
        agents = []
        for agent_id in registry.known_agents():
            try:
                mode = registry.get_agent_mode(agent_id)
            except KeyError:
                continue

            # Get infrastructure if available
            infra = registry.get_running_infrastructure(agent_id)
            live = infra is not None

            # Compute status fields
            active_run_id = None
            pending_approvals = 0
            run_phase = "idle"

            if infra:
                # Get pending approvals count
                pending_approvals = len(infra.approval_hub.pending)

                # Derive run phase based on active state
                if pending_approvals > 0:
                    run_phase = "waiting_approval"
                elif live:
                    run_phase = "sampling"

            # Determine capabilities
            is_local = mode == AgentMode.LOCAL
            capabilities = {"chat": is_local, "agent_loop": is_local}

            agents.append(
                {
                    "id": agent_id,
                    "mode": mode,
                    "live": live,
                    "active_run_id": str(active_run_id) if active_run_id else None,
                    "run_phase": run_phase,
                    "pending_approvals": pending_approvals,
                    "capabilities": capabilities,
                }
            )

        return json.dumps({"agents": agents})

    @server.resource(
        "resource://agents/{agent_id}/state",
        name="agent.state",
        mime_type="application/json",
        description="Sampling state for a local agent",
    )
    async def agent_state(agent_id: AgentID):
        """Raises ValueError if agent is not local or has no runtime."""
        if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
            raise ValueError(f"Agent {agent_id} is not a local agent")

        if (local_runtime := registry.get_local_runtime(agent_id)) is None:
            raise ValueError(f"Agent {agent_id} has no local runtime")

        # Get sampling snapshot from the compositor
        return await local_runtime.running.compositor.sampling_snapshot()

    @server.resource(
        "resource://agents/{agent_id}/snapshot",
        name="agent.snapshot",
        mime_type="application/json",
        description="Full compositor sampling snapshot for a local agent",
    )
    async def agent_snapshot(agent_id: AgentID):
        """Get full compositor sampling snapshot for an agent.

        Returns the complete compositor sampling state including tools, resources,
        and prompts from all mounted servers.

        Raises ValueError if agent is not local or has no runtime.
        """
        if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
            raise ValueError(f"Agent {agent_id} is not a local agent")

        if (local_runtime := registry.get_local_runtime(agent_id)) is None:
            raise ValueError(f"Agent {agent_id} has no local runtime")

        # Delegate to compositor's sampling_snapshot()
        return await local_runtime.running.compositor.sampling_snapshot()

    @server.resource(
        "resource://agents/{agent_id}/mcp/state",
        name="agent.mcp.state",
        mime_type="application/json",
        description="MCP servers state",
    )
    async def agent_mcp_state(agent_id: AgentID):
        """MCP servers state.

        Returns the sampling snapshot from the compositor wrapped in a dict.

        Raises ValueError if agent is not local or has no runtime.
        """
        if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
            raise ValueError(f"Agent {agent_id} is not a local agent")

        if (local_runtime := registry.get_local_runtime(agent_id)) is None:
            raise ValueError(f"Agent {agent_id} has no local runtime")

        compositor = local_runtime.running.compositor
        sampling = await compositor.sampling_snapshot()

        return {"sampling": sampling}

    @server.resource(
        "resource://agents/{agent_id}/approvals/pending",
        name="agent.approvals.pending",
        mime_type="application/json",
        description="Pending approvals for a specific agent",
    )
    async def agent_approvals_pending(agent_id: AgentID) -> AgentApprovalsPending:
        infra = await registry.get_infrastructure(agent_id)
        pending = _convert_pending_approvals(infra.approval_hub.pending)
        return AgentApprovalsPending(agent_id=agent_id, pending=pending)

    @server.resource(
        "resource://approvals/pending",
        name="approvals.pending.global",
        mime_type="application/json",
        description="Global mailbox: all pending approvals across all agents (returns multiple content blocks)",
    )
    async def approvals_pending_global():
        """Each approval is a separate MCP TextResourceContents block.

        Crashes if any agent fails (no exception swallowing).
        """
        content_blocks: list[mcp_types.TextResourceContents] = []

        for agent_id in registry.known_agents():
            infra = await registry.get_infrastructure(agent_id)
            pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)

            for approval in pending_approvals:
                approval_uri = f"resource://agents/{agent_id}/approvals/{approval.tool_call.call_id}"
                # Parse args_json if present
                args = json.loads(approval.tool_call.args_json) if approval.tool_call.args_json else {}
                approval_data = {
                    "agent_id": agent_id,
                    "call_id": approval.tool_call.call_id,
                    "tool": approval.tool_call.name,
                    "args": args,
                    "timestamp": approval.timestamp.isoformat(),
                }
                block = mcp_types.TextResourceContents(
                    uri=approval_uri, mimeType="application/json", text=json.dumps(approval_data)
                )
                content_blocks.append(block)

        return mcp_types.ReadResourceResult(contents=content_blocks)

    @server.resource(
        "resource://agents/{agent_id}/approvals/history",
        name="agent.approvals.history",
        mime_type="application/json",
        description="Historical approval timeline for an agent (activity log)",
    )
    async def agent_approvals_history(agent_id: AgentID) -> AgentApprovalsHistory:
        """Includes both pending (not yet decided) and completed approvals."""
        infra = await registry.get_infrastructure(agent_id)

        # Get all tool call records (limit to recent 100)
        # Note: list_tool_calls doesn't support agent_id filtering yet, so we get all and filter
        all_records = await infra.approval_engine.persistence.list_tool_calls()
        agent_records = [r for r in all_records if r.agent_id == agent_id][-100:]

        # Convert to history entries (filters out PENDING records)
        completed_entries = []
        for record in agent_records:
            entry = _convert_tool_call_record_to_history(record)
            if entry is not None:
                completed_entries.append(entry)

        pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)

        # Count includes both completed and pending
        total_count = len(completed_entries) + len(pending_approvals)

        return AgentApprovalsHistory(
            agent_id=agent_id, timeline=completed_entries, pending=pending_approvals, count=total_count
        )

    @server.resource(
        "resource://agents/{agent_id}/policy/proposals",
        name="agent.policy.proposals",
        mime_type="application/json",
        description="Policy proposals for an agent (links to full proposals in policy server)",
    )
    async def agent_policy_proposals(agent_id: AgentID) -> AgentPolicyProposals:
        """Lists policy proposals with URIs to access full content in policy server."""
        infra = await registry.get_infrastructure(agent_id)
        proposals = await infra.approval_engine.persistence.list_policy_proposals(agent_id)

        proposal_infos = [
            PolicyProposalInfo(
                id=p.id,
                status=p.status,
                created_at=p.created_at,
                decided_at=p.decided_at,
                proposal_uri=f"resource://approval-policy/proposals/{p.id}",
            )
            for p in proposals
        ]

        return AgentPolicyProposals(
            agent_id=agent_id, proposals=proposal_infos, active_policy_uri="resource://approval-policy/policy.py"
        )

    @server.resource(
        "resource://agents/{agent_id}/policy/state",
        name="agent.policy.state",
        mime_type="application/json",
        description="Policy state and proposals for an agent",
    )
    async def agent_policy_state_resource(agent_id: AgentID) -> AgentPolicyState:
        """Get policy state and proposals for an agent."""
        infra = await registry.get_infrastructure(agent_id)
        engine = infra.approval_engine

        # Get current policy
        content, policy_id = engine.get_policy()

        # Get proposals
        db_proposals = await engine.persistence.list_policy_proposals(agent_id)
        proposals = [{"id": p.id, "status": p.status} for p in db_proposals]

        policy_data = {"content": content, "id": policy_id, "proposals": proposals}

        return AgentPolicyState(
            agent_id=agent_id, policy=policy_data, active_policy_uri="resource://approval-policy/policy.py"
        )

    @server.resource(
        "resource://agents/{agent_id}/ui/state",
        name="agent.ui.state",
        mime_type="application/json",
        description="UI state (optional, only if UI server attached)",
    )
    async def agent_ui_state_resource(agent_id: AgentID) -> str:
        """UI state (optional, only if UI server attached)."""
        runtime = registry.get(agent_id)
        if not runtime or not runtime.runtime or not runtime.runtime.session:
            raise ValueError(f"Agent {agent_id} has no session")

        ui_state = runtime.runtime.session.ui_state

        return json.dumps({"seq": ui_state.seq, "state": ui_state.model_dump()})

    @server.resource(
        "resource://agents/{agent_id}/info",
        name="agent.info",
        mime_type="application/json",
        description="Basic agent metadata (mode, model, status) - use specific resources for details",
    )
    async def agent_info(agent_id: AgentID) -> AgentInfoDetailed:
        """Get basic agent metadata NOT available from other MCP resources.

        Returns only agent mode, model, and runtime status.
        For additional data, query the appropriate MCP resources:
        - Compositor: resource://agents/{id}/snapshot
        - Policy: resource://approval-policy/policy.py (per-agent server)
        - Approvals: resource://agents/{id}/approvals/pending, resource://agents/{id}/approvals/history
        """
        mode = registry.get_agent_mode(agent_id)

        # Determine model and status
        model: str | None = None
        status = ServerStatus.STOPPED

        local_runtime = registry.get_local_runtime(agent_id)
        if local_runtime is not None:
            model = local_runtime.model
            status = ServerStatus.RUNNING

        return AgentInfoDetailed(agent_id=agent_id, mode=mode, model=model, status=status)

    @server.resource(
        "resource://agents/{agent_id}/session/state",
        name="agent.session.state",
        mime_type="application/json",
        description="Agent session state and transcript (local agents only)",
    )
    async def agent_session_state_resource(agent_id: AgentID) -> dict:
        """Agent session state and transcript.

        Returns the current session state including active run information and transcript.
        Only available for local agents with an active session.

        Raises ValueError if agent is not local or has no session.
        """
        if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
            raise ValueError(f"Agent {agent_id} is not a local agent")

        local_runtime = registry.get_local_runtime(agent_id)
        if local_runtime is None or local_runtime.session is None:
            raise ValueError(f"Agent {agent_id} has no session")

        session = local_runtime.session

        # Build and return session snapshot
        return {
            "session_state": {
                "session_id": session._manager._session_id,
                "version": "1.0.0",
                "active_run_id": str(session.active_run.run_id) if session.active_run else None,
                "run_counter": session._run_counter,
            },
            "run_state": {
                "run_id": str(session.active_run.run_id),
                "status": ServerStatus.RUNNING,
                "started_at": session.active_run.started_at.isoformat(),
            }
            if session.active_run
            else None,
        }

    @server.resource(
        "resource://presets/list",
        name="presets.list",
        mime_type="application/json",
        description="Available agent presets",
    )
    async def presets_list() -> PresetsList:
        """List all available agent presets.

        Loads presets from configured directories (ADGN_AGENT_PRESETS_DIR or XDG config).
        Returns preset names and descriptions.
        """
        presets = discover_presets(os.getenv("ADGN_AGENT_PRESETS_DIR"))
        summaries = [PresetSummary(name=name, description=p.description) for name, p in presets.items()]
        return PresetsList(presets=summaries)

    # Tools

    @server.tool()
    async def decide_approval(
        agent_id: AgentID, call_id: str, decision: UserApprovalDecision, reason: str | None = None
    ) -> None:
        """Unified tool for handling approval decisions.

        Resolves a pending approval with one of three outcomes:
        - "approve": Allow the tool call to proceed (USER_APPROVE)
        - "deny_continue": Deny the tool call but continue the turn (USER_DENY_CONTINUE)
        - "deny_abort": Deny the tool call and abort the entire turn (USER_DENY_ABORT)

        Args:
            agent_id: The target agent ID
            call_id: The approval request call ID
            decision: The approval decision kind ("approve", "deny_continue", or "deny_abort")
            reason: Optional reason for denial (required for deny_continue and deny_abort)
        """
        infra = await registry.get_infrastructure(agent_id)

        # Map UserApprovalDecision to handler decision type
        if decision == UserApprovalDecision.APPROVE:
            handler_decision = ContinueDecision()
        elif decision == UserApprovalDecision.DENY_CONTINUE:
            handler_decision = DenyContinueDecision(reason=reason)
        elif decision == UserApprovalDecision.DENY_ABORT:
            handler_decision = AbortTurnDecision(reason=reason)
        else:
            raise ValueError(f"Invalid approval decision: {decision}")

        infra.approval_hub.resolve(call_id, handler_decision)

        # Broadcast resource updates
        await server.broadcast_resource_updated(resources.agent_approvals_pending(agent_id))
        await server.broadcast_resource_updated(resources.agent_approvals_history(agent_id))
        await server.broadcast_resource_updated(resources.APPROVALS_PENDING_GLOBAL)

    @server.tool()
    async def abort_agent(agent_id: AgentID) -> None:
        """Raises ValueError if agent is not local or has no agent loop."""
        if registry.get_agent_mode(agent_id) != AgentMode.LOCAL:
            raise ValueError(f"Agent {agent_id} is not a local agent (cannot abort)")

        local_runtime = registry.get_local_runtime(agent_id)
        if local_runtime is None or local_runtime.agent is None:
            raise ValueError(f"Agent {agent_id} has no agent loop")

        await local_runtime.agent.abort()  # type: ignore[attr-defined]  # TODO: Implement abort() on MiniCodex

    @server.tool()
    async def prompt(agent_id: AgentID, message: str) -> SimpleOk:
        """Send a user message to an agent (via chat.human server).

        Routes the message to the agent's chat.human MCP server by calling
        the post tool. Returns immediately after queueing the message.

        Args:
            agent_id: The target agent ID.
            message: The user message to send.

        Returns:
            SimpleOk indicating successful message delivery.
        """
        infra = await registry.get_infrastructure(agent_id)
        client = infra.compositor.get_child_client("chat.human")
        await client.call_tool("post", {"message": message})
        return SimpleOk(ok=True)

    @server.tool()
    async def abort_run(agent_id: AgentID) -> SimpleOk:
        """Abort a running agent (alias for abort_agent).

        Requests immediate termination of the agent's active loop.
        This is a semantic alias for abort_agent that returns SimpleOk for consistency.

        Args:
            agent_id: The target agent ID.

        Returns:
            SimpleOk indicating successful abort request.

        Raises:
            ValueError: If agent is not local or has no agent loop.
        """
        await abort_agent(agent_id)
        return SimpleOk(ok=True)

    @server.tool()
    async def update_mcp_config(agent_id: AgentID, config: dict) -> SimpleOk:
        """Update MCP server configuration for an agent.

        Converges agent's MCP mounts to exactly match the provided configuration
        (full replacement: unmounts servers not in config, mounts new servers).
        """
        infra = await registry.get_infrastructure(agent_id)
        cfg = MCPConfig.model_validate(config)
        await infra.compositor.reconfigure(cfg)
        return SimpleOk(ok=True)

    @server.tool()
    async def attach_server(agent_id: AgentID, name: str, spec: dict) -> SimpleOk:
        """Attach a new MCP server to an agent.

        Mounts a single MCP server with the given name and specification.
        Raises ValueError if a server with that name is already mounted.
        """
        infra = await registry.get_infrastructure(agent_id)
        server_spec = TypeAdapter(MCPServerTypes).validate_python(spec)
        await infra.compositor.mount_server(name, server_spec)
        return SimpleOk(ok=True)

    @server.tool()
    async def detach_server(agent_id: AgentID, name: str) -> SimpleOk:
        """Detach an MCP server from an agent.

        Unmounts a single MCP server by name. Raises RuntimeError if the
        server is pinned (system servers cannot be unmounted).
        """
        infra = await registry.get_infrastructure(agent_id)
        await infra.compositor.unmount_server(name)
        return SimpleOk(ok=True)

    @server.tool()
    async def set_policy(agent_id: AgentID, policy_text: str) -> SimpleOk:
        """Set the active policy text for an agent.

        Directly sets the policy source code after validation via the
        approval policy admin server. The policy is self-checked before
        activation to ensure it's valid Python and can execute properly.

        Raises ValueError if agent not found.
        Raises RuntimeError if policy validation fails.
        """
        infra = await registry.get_infrastructure(agent_id)
        await infra.policy_approver.set_policy_text(SetPolicyTextArgs(source=policy_text))
        return SimpleOk(ok=True)

    @server.tool()
    async def approve_proposal(agent_id: AgentID, proposal_id: str) -> SimpleOk:
        """Approve a pending policy proposal for an agent.

        Approves a policy proposal by ID, which activates the proposed
        policy as the agent's active policy. The proposal must be in
        PENDING status.

        Raises ValueError if agent or proposal not found.
        Raises RuntimeError if proposal is not in PENDING status.
        """
        infra = await registry.get_infrastructure(agent_id)
        await infra.policy_approver.approve_proposal(ApproveProposalArgs(id=proposal_id))
        return SimpleOk(ok=True)

    @server.tool()
    async def reject_proposal(agent_id: AgentID, proposal_id: str, reason: str) -> SimpleOk:
        """Reject a pending policy proposal for an agent.

        Rejects a policy proposal by ID with an optional reason. The
        proposal must be in PENDING status. The proposal remains in the
        database but is marked as rejected.

        Raises ValueError if agent or proposal not found.
        Raises RuntimeError if proposal is not in PENDING status.
        """
        infra = await registry.get_infrastructure(agent_id)
        await infra.policy_approver.reject_proposal(RejectProposalArgs(id=proposal_id, reason=reason))
        return SimpleOk(ok=True)

    @server.tool()
    async def create_agent() -> AgentBrief:
        """Create a new agent.

        Generates a unique agent ID and initializes infrastructure for a new agent.
        The agent will be ready to accept connections and process requests.

        Returns:
            AgentBrief with the newly created agent's ID and initial state.
        """
        # Generate unique agent ID
        agent_id = AgentID(f"agent-{uuid4().hex[:8]}")

        # Create infrastructure for the agent
        await registry.create_agent(agent_id)

        # Return agent brief with the created agent's ID
        return AgentBrief(id=agent_id)

    @server.tool()
    async def delete_agent(agent_id: AgentID) -> SimpleOk:
        """Delete an agent and clean up its infrastructure.

        Removes the agent from the registry, closes all running infrastructure,
        and releases associated resources. The agent can no longer be accessed
        after deletion.

        Args:
            agent_id: ID of the agent to delete.

        Returns:
            SimpleOk confirming successful deletion.

        Raises:
            KeyError: If the agent is not found in the registry.
        """
        await registry.remove_agent(agent_id)
        return SimpleOk(ok=True)

    @server.tool()
    async def boot_agent(agent_id: AgentID) -> SimpleOk:
        """Ensure an agent is booted and its infrastructure is running.

        Creates or resumes the agent's infrastructure to ensure it's ready
        for operation. If the agent is already running, this is a no-op.

        Args:
            agent_id: ID of the agent to boot.

        Returns:
            SimpleOk confirming the agent is ready.

        Raises:
            KeyError: If the agent is not found in the registry.
        """
        await registry.ensure_live(agent_id)
        return SimpleOk(ok=True)

    # Wire up notifications
    # For approval changes: wire ApprovalHub notifier to broadcast MCP resource updates
    # For policy changes: wire policy engine notifier to broadcast MCP resource updates

    for agent_id in registry.known_agents():
        infra = await registry.get_infrastructure(agent_id)

        # Closure captures agent_id for this specific agent
        def make_policy_notifier(aid: str):
            def notifier(uri: str):
                # Notifier is sync, schedule broadcast in event loop
                loop = asyncio.get_running_loop()
                _task = loop.create_task(server.broadcast_resource_updated(uri))
                # Don't await task - fire and forget notification
                _task.add_done_callback(
                    lambda t: logger.debug(f"Broadcast complete for {uri}")
                    if not t.exception()
                    else logger.warning(f"Broadcast failed for {uri}: {t.exception()}")
                )

            return notifier

        infra.approval_engine.set_notifier(make_policy_notifier(agent_id))

        # Wire approval hub notifier to broadcast resource updates
        def make_approval_hub_notifier(aid: AgentID):
            def notifier():
                # Notifier is sync, schedule broadcast in event loop
                loop = asyncio.get_running_loop()
                # Broadcast all relevant approval resources
                uris = [
                    resources.agent_approvals_pending(aid),
                    resources.agent_approvals_history(aid),
                    resources.APPROVALS_PENDING_GLOBAL,
                ]
                for uri in uris:
                    _task = loop.create_task(server.broadcast_resource_updated(uri))
                    _task.add_done_callback(
                        lambda t, u=uri: logger.debug(f"Broadcast complete for {u}")
                        if not t.exception()
                        else logger.warning(f"Broadcast failed for {u}: {t.exception()}")
                    )

            return notifier

        infra.approval_hub.set_notifier(make_approval_hub_notifier(agent_id))

        # Wire UI state notifications (only for local agents with session)
        local_runtime = registry.get_local_runtime(agent_id)
        if local_runtime is not None and local_runtime.session is not None:

            def make_ui_state_notifier(aid: AgentID):
                def notifier():
                    # Notifier is sync, schedule broadcast in event loop
                    loop = asyncio.get_running_loop()
                    _task = loop.create_task(server.broadcast_resource_updated(resources.agent_ui_state(aid)))
                    # Don't await task - fire and forget notification
                    _task.add_done_callback(
                        lambda t: logger.debug(f"UI state broadcast complete for {aid}")
                        if not t.exception()
                        else logger.warning(f"UI state broadcast failed for {aid}: {t.exception()}")
                    )

                return notifier

            local_runtime.session.set_ui_state_notifier(make_ui_state_notifier(agent_id))

            # Wire session state notifications
            def make_session_state_notifier(aid: AgentID):
                def notifier():
                    # Notifier is sync, schedule broadcast in event loop
                    loop = asyncio.get_running_loop()
                    _task = loop.create_task(server.broadcast_resource_updated(resources.agent_session_state(aid)))
                    # Don't await task - fire and forget notification
                    _task.add_done_callback(
                        lambda t: logger.debug(f"Session state broadcast complete for {aid}")
                        if not t.exception()
                        else logger.warning(f"Session state broadcast failed for {aid}: {t.exception()}")
                    )

                return notifier

            local_runtime.session._manager.set_session_state_notifier(make_session_state_notifier(agent_id))

        # Wire compositor mount events to broadcast MCP state resource updates
        def make_mount_listener(aid: AgentID):
            async def on_mount_change(name: str, action: MountEvent) -> None:
                # Broadcast resource update for the agent's MCP state when servers mount/unmount
                if action in (MountEvent.MOUNTED, MountEvent.UNMOUNTED):
                    await server.broadcast_resource_updated(resources.agent_mcp_state(aid))

            return on_mount_change

        infra.compositor.add_mount_listener(make_mount_listener(agent_id))

    # Wire registry notifier to broadcast agents list updates when agents are created/deleted
    async def registry_notifier(uri: str):
        await server.broadcast_resource_updated(uri)

    registry.set_notifier(registry_notifier)

    return server
