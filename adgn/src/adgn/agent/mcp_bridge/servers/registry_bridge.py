"""Registry server for agents bridge (exposes global agent registry resources)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from adgn.agent.mcp_bridge.servers.types import RunPhase
from adgn.agent.mcp_bridge.types import AgentID, AgentMode
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

if TYPE_CHECKING:
    from adgn.agent.mcp_bridge.server import InfrastructureRegistry
    from adgn.mcp.compositor.server import Compositor

logger = logging.getLogger(__name__)


class AgentCapabilities(BaseModel):
    """Capabilities available for an agent."""
    chat: bool
    agent_loop: bool


class AgentInfo(BaseModel):
    """Information about a single agent."""
    id: AgentID
    mode: AgentMode
    live: bool
    run_phase: RunPhase
    pending_approvals: int
    capabilities: AgentCapabilities


class AgentsListResponse(BaseModel):
    """Response containing list of all agents."""
    agents: list[AgentInfo]


class AgentRegistryBridgeServer(NotifyingFastMCP):
    """MCP server exposing agent registry resources for agents bridge.

    This server wraps InfrastructureRegistry and provides global agent listing
    and management resources.

    Resources (after mounting):
    - resource://registry/agents/list - List all agents with status
    - resource://registry/agents/{id}/info - Specific agent information
    """

    def __init__(self, registry: InfrastructureRegistry, global_compositor: Compositor | None = None):
        super().__init__(name="registry")
        self._registry = registry
        self._global_compositor = global_compositor
        self._register_resources()

    def _register_resources(self) -> None:
        @self.resource("resource://agents/list", name="agents_list", mime_type="application/json")
        async def list_agents() -> AgentsListResponse:
            """List all agents with detailed status."""
            agents = []
            for agent_id in self._registry.known_agents():
                try:
                    mode = self._registry.get_agent_mode(agent_id)
                except KeyError:
                    continue

                # Get infrastructure if available
                infra = self._registry.get_running_infrastructure(agent_id)
                live = infra is not None

                # Compute status fields
                pending_approvals = 0
                run_phase = RunPhase.IDLE

                if infra:
                    # Get pending approvals count
                    pending_approvals = len(infra.approval_hub.pending)

                    # Derive run phase
                    if pending_approvals > 0:
                        run_phase = RunPhase.WAITING_APPROVAL
                    elif live:
                        run_phase = RunPhase.SAMPLING

                # Determine capabilities
                is_local = mode == AgentMode.LOCAL

                agents.append(
                    AgentInfo(
                        id=agent_id,
                        mode=mode,
                        live=live,
                        run_phase=run_phase,
                        pending_approvals=pending_approvals,
                        capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
                    )
                )

            return AgentsListResponse(agents=agents)

        @self.resource("resource://agents/{agent_id}/info", name="agent_info", mime_type="application/json")
        async def get_agent_info(agent_id: AgentID) -> AgentInfo:
            """Get detailed information about a specific agent."""
            try:
                mode = self._registry.get_agent_mode(agent_id)
            except KeyError:
                raise KeyError(f"Agent {agent_id} not found")

            infra = self._registry.get_running_infrastructure(agent_id)
            live = infra is not None

            pending_approvals = 0
            run_phase = RunPhase.IDLE

            if infra:
                pending_approvals = len(infra.approval_hub.pending)
                if pending_approvals > 0:
                    run_phase = RunPhase.WAITING_APPROVAL
                elif live:
                    run_phase = RunPhase.SAMPLING

            is_local = mode == AgentMode.LOCAL

            return AgentInfo(
                id=agent_id,
                mode=mode,
                live=live,
                run_phase=run_phase,
                pending_approvals=pending_approvals,
                capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
            )

    async def notify_agents_list_changed(self) -> None:
        """Notify that the agents list has changed."""
        await self.broadcast_resource_updated("resource://agents/list")
        await self.broadcast_resource_list_changed()
