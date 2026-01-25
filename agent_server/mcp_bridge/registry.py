"""Infrastructure registry for Phase 5 two-compositor architecture.

Manages:
- Global user-facing compositor
- Per-agent containers (internal and external)
- Agent lifecycle (create, boot, shutdown)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import aiodocker
from fastmcp.mcp_config import MCPConfig

from agent_server.agent_types import AgentID
from agent_server.persist.sqlite import SQLitePersistence
from agent_server.presets import create_agent_from_preset
from agent_server.runtime.container import AgentContainer, build_container
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import OpenAIModelProto

# Mount prefix for agent control (send_prompt, abort)
AGENT_CONTROL_MOUNT_PREFIX = MCPMountPrefix("agent_control")

logger = logging.getLogger(__name__)


def agent_mount_prefix(agent_id: AgentID) -> MCPMountPrefix:
    """Generate MCPMountPrefix for an agent's compositor mount.

    AgentID is already validated to match MCPMountPrefix pattern (^[a-z][a-z0-9_]*$),
    so we just add the "agent_" prefix.

    Args:
        agent_id: Agent identifier (already MCPMountPrefix-compatible)

    Returns:
        Mount prefix for the agent's compositor (e.g., "myagent" → "agent_myagent")
    """
    return MCPMountPrefix(f"agent_{agent_id}")


class CompositorNotInitializedError(RuntimeError):
    """Raised when attempting operations requiring the global compositor before it's set."""


@dataclass
class InfrastructureRegistry:
    """Registry managing global compositor and agent containers.

    The registry owns:
    - Global user-facing compositor (all agents visible to user)
    - Per-agent containers (user + agent compositors)
    - Agent lifecycle operations

    Token routing is handled by TokenRoutingASGI at the ASGI level.
    """

    persistence: SQLitePersistence
    model: str
    client_factory: Callable[[str], OpenAIModelProto]
    async_docker_client: aiodocker.Docker
    mcp_config: MCPConfig  # Base MCP config for new agents
    initial_policy: str | None = None

    # Global user-facing compositor (set by app.py after creation)
    global_compositor: Compositor | None = None

    # Agent containers keyed by agent_id
    _agents: dict[AgentID, AgentContainer] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Track which agents are external (cannot be controlled via UI)
    _external_agents: set[AgentID] = field(default_factory=set)

    def get_agent(self, agent_id: AgentID) -> AgentContainer | None:
        """Get agent container by ID."""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentContainer]:
        """List all agent containers."""
        return list(self._agents.values())

    def is_external(self, agent_id: AgentID) -> bool:
        """Check if agent is external (cannot be controlled via UI)."""
        return agent_id in self._external_agents

    def _ensure_compositor(self) -> Compositor:
        """Return global compositor or raise CompositorNotInitializedError."""
        if (comp := self.global_compositor) is None:
            raise CompositorNotInitializedError("Global compositor not initialized")
        return comp

    async def _register_and_mount_agent(self, container: AgentContainer, *, external: bool) -> None:
        """Register container and mount to global compositor.

        For internal agents, also mounts agent_control server.
        """
        comp = self._ensure_compositor()
        self._agents[container.agent_id] = container

        if not external:
            await self._mount_agent_control(container)

        # Mount agent compositor to global
        if container._compositor is None:
            raise RuntimeError(f"Agent container {container.agent_id} has no compositor after build")

        mount_prefix = agent_mount_prefix(container.agent_id)
        await comp.mount_inproc(mount_prefix, container._compositor)

    async def _mount_agent_control(self, container: AgentContainer) -> None:
        """Mount agent_control server on container's compositor.

        Only for internal agents - provides send_prompt and abort tools.
        """
        if container._compositor is None:
            raise RuntimeError(f"Agent container {container.agent_id} has no compositor for agent_control mount")

        control_server = container.make_control_server()
        await container._compositor.mount_inproc(AGENT_CONTROL_MOUNT_PREFIX, control_server)
        logger.debug(f"Mounted agent_control for internal agent: {container.agent_id}")

    async def _create_container(
        self,
        agent_id: AgentID,
        *,
        mcp_config: MCPConfig | None = None,
        system: str | None = None,
        external: bool = False,
    ) -> AgentContainer:
        """Internal: create a new agent container.

        Args:
            agent_id: Agent identifier
            mcp_config: MCP config (uses default if not provided)
            system: System prompt override
            external: Whether this is an external agent (no agent_control)

        Returns:
            Created AgentContainer (not yet started)
        """
        config = mcp_config or self.mcp_config
        container = await build_container(
            agent_id=agent_id,
            mcp_config=config,
            persistence=self.persistence,
            model=self.model,
            client_factory=self.client_factory,
            with_ui=True,
            system=system,
            async_docker_client=self.async_docker_client,
            initial_policy=self.initial_policy,
        )
        if external:
            self._external_agents.add(agent_id)
        return container

    async def create_agent(self, preset: str | None = None) -> AgentContainer:
        """Create a NEW agent from preset and boot it immediately.

        This is for internal agents only. Creates agent record in DB,
        boots the container, and mounts user compositor to global.

        Args:
            preset: Preset name to use for agent configuration

        Returns:
            Booted AgentContainer

        Raises:
            CompositorNotInitializedError: If global compositor is not set
        """
        self._ensure_compositor()  # Validate early

        async with self._lock:
            # Create agent record in DB from preset
            agent_id, mcp_config, system = await create_agent_from_preset(
                persistence=self.persistence, preset_name=preset, base_mcp_config=self.mcp_config
            )

            # Build and start container
            container = await self._create_container(agent_id, mcp_config=mcp_config, system=system, external=False)
            await self._register_and_mount_agent(container, external=False)

            return container

    async def boot_agent(self, agent_id: AgentID) -> AgentContainer:
        """Boot an EXISTING agent that has state in DB.

        Used for internal agents only. Loads agent from persistence,
        boots the container, and mounts user compositor to global.

        Args:
            agent_id: Agent identifier (must exist in DB)

        Returns:
            Booted AgentContainer

        Raises:
            CompositorNotInitializedError: If global compositor is not set
            KeyError: If agent doesn't exist in DB
        """
        self._ensure_compositor()  # Validate early

        async with self._lock:
            if existing := self._agents.get(agent_id):
                return existing

            if (row := await self.persistence.get_agent(agent_id)) is None:
                raise KeyError(f"Agent not found: {agent_id}")

            container = await self._create_container(agent_id, mcp_config=row.mcp_config, external=False)
            await self._register_and_mount_agent(container, external=False)

            return container

    async def create_external_agent(self, agent_id: AgentID) -> AgentContainer:
        """Create an external agent's container at startup.

        External agents are created eagerly from tokens config.
        Like boot_agent(), this ALSO mounts user compositor to global
        so the user sees all agents (internal + external) in the same UI.

        External agents have limitations:
        - No agent_control server (can't send_prompt, can't abort)
        - User can only view state and approve/reject

        Args:
            agent_id: Agent identifier

        Returns:
            Created AgentContainer

        Raises:
            CompositorNotInitializedError: If global compositor is not set
        """
        self._ensure_compositor()  # Validate early

        async with self._lock:
            if existing := self._agents.get(agent_id):
                return existing

            # Check if agent exists in DB, use its config or fall back to base
            row = await self.persistence.get_agent(agent_id)
            mcp_config = row.mcp_config if row else self.mcp_config

            container = await self._create_container(agent_id, mcp_config=mcp_config, external=True)
            await self._register_and_mount_agent(container, external=True)

            logger.info(f"Created external agent: {agent_id}")
            return container

    async def shutdown_agent(self, agent_id: AgentID) -> None:
        """Shutdown agent and unmount its user compositor.

        Raises:
            CompositorNotInitializedError: If global compositor is not set
            KeyError: If agent is not running
        """
        comp = self._ensure_compositor()

        async with self._lock:
            if agent_id not in self._agents:
                raise KeyError(f"Agent not running: {agent_id}")

            # Unmount from global
            try:
                mount_prefix = agent_mount_prefix(agent_id)
                await comp.unmount_server(mount_prefix)
            except KeyError:
                logger.debug(f"Agent {agent_id} already unmounted")

            # Close container
            container = self._agents.pop(agent_id)
            await container.close()

            # Clean up external tracking
            self._external_agents.discard(agent_id)

            logger.info(f"Shutdown agent: {agent_id}")

    async def shutdown_all(self) -> None:
        """Shutdown all agents.

        Raises:
            CompositorNotInitializedError: If global compositor is not set and agents exist
        """
        if not self._agents:
            return
        self._ensure_compositor()

        for agent_id in list(self._agents.keys()):
            await self.shutdown_agent(agent_id)
