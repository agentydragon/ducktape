"""Agents management MCP server.

Provides tools and resources for managing agents:
- list resource: all agents with state
- presets resource: available presets
- create_agent tool: create new agent from preset
- delete_agent tool: delete an agent
- boot_agent tool: boot existing agent from DB
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from fastmcp.resources import FunctionResource
from pydantic import BaseModel, Field

from agent_server.presets import discover_presets
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

if TYPE_CHECKING:
    from agent_server.mcp_bridge.registry import InfrastructureRegistry

logger = logging.getLogger(__name__)


# ---- Resource models ---------------------------------------------------------


class AgentInfo(BaseModel):
    """Agent information for list resource."""

    id: str
    preset: str | None = None
    external: bool = False
    booted: bool = False


class PresetInfo(BaseModel):
    """Preset information for presets resource."""

    name: str
    description: str | None = None


# ---- Tool I/O models ---------------------------------------------------------


class CreateAgentInput(OpenAIStrictModeBaseModel):
    """Input for create_agent tool."""

    preset: str | None = Field(description="Preset name to use (None = use default preset)")


class CreateAgentOutput(BaseModel):
    """Output for create_agent tool."""

    id: str
    status: Literal["created"]
    preset: str


class DeleteAgentInput(OpenAIStrictModeBaseModel):
    """Input for delete_agent tool."""

    agent_id: str = Field(description="Agent ID to delete")


class DeleteAgentOutput(BaseModel):
    """Output for delete_agent tool."""

    id: str
    status: Literal["deleted"]


class BootAgentInput(OpenAIStrictModeBaseModel):
    """Input for boot_agent tool."""

    agent_id: str = Field(description="Agent ID to boot (must exist in DB)")


class BootAgentOutput(BaseModel):
    """Output for boot_agent tool."""

    id: str
    status: Literal["booted"]


# ---- Server class ----------------------------------------------------------


class AgentsManagementServer(EnhancedFastMCP):
    """Agents management MCP server with typed resource/tool access.

    Provides tools and resources for managing agents:
    - list resource: all agents with state
    - presets resource: available presets
    - create_agent tool: create new agent from preset
    - delete_agent tool: delete an agent
    - boot_agent tool: boot existing agent from DB
    """

    # Resource attributes (stashed results of @resource decorator - single source of truth for URI access)
    list_resource: FunctionResource
    presets_resource: FunctionResource

    # Tool references (assigned in __init__)
    create_agent_tool: FlatTool[Any, Any]
    delete_agent_tool: FlatTool[Any, Any]
    boot_agent_tool: FlatTool[Any, Any]

    def __init__(self, registry: InfrastructureRegistry):
        """Create agents management server bound to an infrastructure registry.

        Args:
            registry: Infrastructure registry for managing agent lifecycle
        """
        super().__init__("Agents Management MCP Server")
        self._registry = registry

        # Register resources and stash the results
        async def list_agents() -> list[AgentInfo]:
            """List all agents with their state.

            Returns agents from both:
            - Running containers in registry
            - Persisted agents in DB (not yet booted)
            """
            agents: list[AgentInfo] = []

            # Get running agents from registry
            for container in self._registry.list_agents():
                agent_id = container.agent_id
                # Get preset from persistence
                row = await self._registry.persistence.get_agent(agent_id)
                preset = row.metadata.preset if (row and row.metadata) else None
                agents.append(
                    AgentInfo(id=agent_id, preset=preset, external=self._registry.is_external(agent_id), booted=True)
                )

            # Get persisted agents that aren't running
            running_ids = {c.agent_id for c in self._registry.list_agents()}
            persisted = await self._registry.persistence.list_agents()
            for row in persisted:
                if row.id not in running_ids:
                    preset = row.metadata.preset if row.metadata else None
                    agents.append(AgentInfo(id=row.id, preset=preset, external=False, booted=False))

            return agents

        self.list_resource = cast(FunctionResource, self.resource("agents://list")(list_agents))

        async def list_presets() -> list[PresetInfo]:
            """List available agent presets."""
            presets = discover_presets()
            return [PresetInfo(name=p.name, description=p.description) for p in presets.values()]

        self.presets_resource = cast(FunctionResource, self.resource("agents://presets")(list_presets))

        # Register tools - names derived from function names
        async def create_agent(input: CreateAgentInput) -> CreateAgentOutput:
            """Create a new agent from a preset and boot it."""
            container = await self._registry.create_agent(preset=input.preset)
            await self.broadcast_resource_updated(self.list_resource.uri)
            return CreateAgentOutput(id=container.agent_id, status="created", preset=input.preset or "default")

        self.create_agent_tool = self.flat_model()(create_agent)

        async def delete_agent(input: DeleteAgentInput) -> DeleteAgentOutput:
            """Delete an agent."""
            await self._registry.shutdown_agent(input.agent_id)
            await self._registry.persistence.delete_agent(input.agent_id)
            await self.broadcast_resource_updated(self.list_resource.uri)
            return DeleteAgentOutput(id=input.agent_id, status="deleted")

        self.delete_agent_tool = self.flat_model()(delete_agent)

        async def boot_agent(input: BootAgentInput) -> BootAgentOutput:
            """Boot an existing agent from the database."""
            container = await self._registry.boot_agent(input.agent_id)
            await self.broadcast_resource_updated(self.list_resource.uri)
            return BootAgentOutput(id=container.agent_id, status="booted")

        self.boot_agent_tool = self.flat_model()(boot_agent)
