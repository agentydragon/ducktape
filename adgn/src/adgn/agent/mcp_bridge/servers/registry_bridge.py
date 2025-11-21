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
        self._register_tools()

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

    def _register_tools(self) -> None:
        @self.tool()
        async def create_agent(agent_id: AgentID) -> dict:
            """Create a new agent and mount its compositor.

            Args:
                agent_id: Unique identifier for the new agent

            Returns:
                Dictionary with agent_id and status
            """
            # Create agent infrastructure
            await self._registry.create_agent(agent_id)

            # Dynamically mount the new agent's compositor if we have a global compositor
            if self._global_compositor is not None:
                from adgn.agent.mcp_bridge.compositor_factory import mount_agent_compositor_dynamically

                await mount_agent_compositor_dynamically(
                    global_compositor=self._global_compositor,
                    agent_id=agent_id,
                    registry=self._registry
                )

            # Notify that agents list changed
            await self.notify_agents_list_changed()

            return {"agent_id": agent_id, "status": "created"}

        @self.tool()
        async def delete_agent(agent_id: AgentID) -> dict:
            """Delete an agent and unmount its compositor.

            Args:
                agent_id: ID of the agent to delete

            Returns:
                Dictionary with agent_id and status
            """
            # Dynamically unmount the agent's compositor if we have a global compositor
            if self._global_compositor is not None:
                from adgn.agent.mcp_bridge.compositor_factory import unmount_agent_compositor_dynamically

                await unmount_agent_compositor_dynamically(
                    global_compositor=self._global_compositor,
                    agent_id=agent_id
                )

            # Remove agent infrastructure
            await self._registry.remove_agent(agent_id)

            # Notify that agents list changed
            await self.notify_agents_list_changed()

            return {"agent_id": agent_id, "status": "deleted"}

    async def notify_agents_list_changed(self) -> None:
        """Notify that the agents list has changed."""
        await self.broadcast_resource_updated("resource://agents/list")
        await self.broadcast_resource_list_changed()
