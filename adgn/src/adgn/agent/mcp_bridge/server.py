"""HTTP MCP Bridge Server - exposes Compositor over HTTP/SSE transport.

This is a standard MCP server (Compositor) exposed via HTTP transport.
External agents connect using MCP-over-HTTP and get policy-gated access to tools.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from docker import DockerClient
from pydantic import BaseModel
from fastapi import FastAPI, Request, Response
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig
from starlette.middleware.base import BaseHTTPMiddleware

from adgn.agent.mcp_bridge.auth import TokenAuthMiddleware, TokenMapping
from adgn.agent.mcp_bridge.servers.types import RunPhase
from adgn.agent.mcp_bridge.types import AgentID, AgentMode
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.runtime.infrastructure import MCPInfrastructure, RunningInfrastructure
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

if TYPE_CHECKING:
    from adgn.agent.runtime.local_runtime import LocalAgentRuntime

logger = logging.getLogger(__name__)


@dataclass
class RunningAgent:
    """All infrastructure for a running agent (single point of optionality)."""

    running: RunningInfrastructure
    compositor_app: FastAPI
    mode: AgentMode
    local_runtime: LocalAgentRuntime | None  # None for bridge agents


@dataclass
class AgentEntry:
    """Registry entry with lock-protected optional agent infrastructure."""

    agent: RunningAgent | None = None
    creation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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


async def create_bridge_infrastructure(
    agent_id: AgentID,
    persistence: SQLitePersistence,
    docker_client: DockerClient,
    mcp_config: MCPConfig,
    initial_policy: str | None = None,
):
    """Create RunningInfrastructure for external agent HTTP bridge."""
    builder = MCPInfrastructure(
        agent_id=agent_id, persistence=persistence, docker_client=docker_client, initial_policy=initial_policy
    )

    return await builder.start(mcp_config)


class InfrastructureRegistry(NotifyingFastMCP):
    """Shared registry for managing per-agent infrastructure with MCP server.

    MCP Resources:
    - resource://agents/list - List all agents with status
    - resource://agents/{id}/info - Specific agent information

    MCP Tools:
    - create_agent(agent_id) - Create a new agent and mount its compositor
    - delete_agent(agent_id) - Delete an agent and unmount its compositor
    """

    def __init__(
        self,
        persistence: SQLitePersistence,
        docker_client: DockerClient,
        mcp_config: MCPConfig,
        initial_policy: str | None,
        global_compositor=None,
    ):
        super().__init__(name="registry")
        self.persistence = persistence
        self.docker_client = docker_client
        self.mcp_config = mcp_config
        self.initial_policy = initial_policy
        self._agents: defaultdict[AgentID, AgentEntry] = defaultdict(AgentEntry)
        self._global_compositor = global_compositor
        self._register_resources()
        self._register_tools()

    async def get_or_create_infrastructure(self, agent_id: AgentID) -> tuple[RunningInfrastructure, FastAPI]:
        """Get or create infrastructure for an agent_id (creates bridge agent).

        Returns both infrastructure and app as a tuple for tests and cases where
        both are needed. Use get_compositor_app() if you only need the app, or
        get_infrastructure() if you only need the infrastructure (without creation).
        """
        entry = self._agents[agent_id]

        async with entry.creation_lock:
            if entry.agent is not None:
                return (entry.agent.running, entry.agent.compositor_app)

            logger.info(f"Creating infrastructure for agent_id={agent_id}")
            running = await create_bridge_infrastructure(
                agent_id=agent_id,
                persistence=self.persistence,
                docker_client=self.docker_client,
                mcp_config=self.mcp_config,
                initial_policy=self.initial_policy,
            )

            await running.__aenter__()

            compositor_app: FastAPI = running.compositor.http_app()  # type: ignore[assignment]

            entry.agent = RunningAgent(
                running=running, compositor_app=compositor_app, mode=AgentMode.BRIDGE, local_runtime=None
            )

            logger.info(f"Infrastructure ready for agent_id={agent_id}")
            return (running, compositor_app)

    async def get_compositor_app(self, agent_id: AgentID) -> FastAPI:
        """Get compositor app for an agent_id."""
        _, app = await self.get_or_create_infrastructure(agent_id)
        return app

    def get_running_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure | None:
        """Get running infrastructure if it exists (doesn't create)."""
        entry = self._agents.get(agent_id)
        return entry.agent.running if entry and entry.agent else None

    def known_agents(self) -> list[AgentID]:
        return list(self._agents.keys())

    async def get_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure:
        """Raises KeyError if agent not in registry or not yet initialized."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        agent = self._agents[agent_id].agent
        if agent is None:
            raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
        return agent.running

    def get_agent_mode(self, agent_id: AgentID) -> AgentMode:
        """Raises KeyError if agent not in registry or not yet initialized."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        agent = self._agents[agent_id].agent
        if agent is None:
            raise KeyError(f"Agent {agent_id} mode not yet initialized")
        return agent.mode

    def get_local_runtime(self, agent_id: AgentID) -> LocalAgentRuntime | None:
        """Returns None if agent is not local. Raises KeyError if agent not in registry."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        return agent.local_runtime if (agent := self._agents[agent_id].agent) else None

    def register_local_agent(
        self,
        agent_id: AgentID,
        running: RunningInfrastructure,
        compositor_app: FastAPI,
        local_runtime: LocalAgentRuntime,
    ) -> None:
        self._agents[agent_id].agent = RunningAgent(
            running=running, compositor_app=compositor_app, mode=AgentMode.LOCAL, local_runtime=local_runtime
        )

    async def create_agent(self, agent_id: AgentID) -> RunningInfrastructure:
        """Create and initialize infrastructure for a new agent.

        Returns the running infrastructure for the agent.
        """
        running, _ = await self.get_or_create_infrastructure(agent_id)
        # Notify that agent list changed
        await self.notify_agents_list_changed()
        return running

    async def ensure_live(self, agent_id: AgentID) -> RunningInfrastructure:
        """Ensure agent infrastructure exists, creating if necessary.

        Returns the running infrastructure for the agent.
        Raises KeyError if agent does not exist in registry.
        """
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        running, _ = await self.get_or_create_infrastructure(agent_id)
        return running

    async def remove_agent(self, agent_id: AgentID) -> None:
        """Remove and clean up agent infrastructure.

        Closes the running infrastructure and removes the agent from the registry.
        """
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")

        agent = self._agents[agent_id].agent
        if agent is not None:
            await agent.running.close()

        del self._agents[agent_id]

        await self.notify_agents_list_changed()

    def _register_resources(self) -> None:
        @self.resource("resource://agents/list", name="agents_list", mime_type="application/json")
        async def list_agents() -> AgentsListResponse:
            """List all agents with detailed status."""
            agents = []
            for agent_id in self.known_agents():
                try:
                    mode = self.get_agent_mode(agent_id)
                except KeyError:
                    continue

                # Get infrastructure if available
                infra = self.get_running_infrastructure(agent_id)
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
                mode = self.get_agent_mode(agent_id)
            except KeyError:
                raise KeyError(f"Agent {agent_id} not found")

            infra = self.get_running_infrastructure(agent_id)
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

            Returns:
                Dictionary with agent_id and status
            """
            # Create agent infrastructure
            await self.create_agent(agent_id)

            # Dynamically mount the new agent's compositor if we have a global compositor
            if self._global_compositor is not None:
                from adgn.agent.mcp_bridge.compositor_factory import mount_agent_compositor_dynamically

                await mount_agent_compositor_dynamically(
                    global_compositor=self._global_compositor,
                    agent_id=agent_id,
                    registry=self
                )

            # Notify that agents list changed
            await self.notify_agents_list_changed()

            return {"agent_id": agent_id, "status": "created"}

        @self.tool()
        async def delete_agent(agent_id: AgentID) -> dict:
            """Delete an agent and unmount its compositor.

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
            await self.remove_agent(agent_id)

            # Notify that agents list changed
            await self.notify_agents_list_changed()

            return {"agent_id": agent_id, "status": "deleted"}

    async def notify_agents_list_changed(self) -> None:
        """Notify that the agents list has changed."""
        await self.broadcast_resource_updated("resource://agents/list")
        await self.broadcast_resource_list_changed()


async def create_mcp_server_app(auth_tokens_path: Path, registry: InfrastructureRegistry) -> FastAPI:
    """Create token-authenticated MCP server app.

    Routes MCP-over-HTTP requests to per-agent compositor apps based on token.
    """

    class CompositorRoutingMiddleware(BaseHTTPMiddleware):
        """Routes requests to the appropriate agent's compositor app."""

        async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            agent_id = request.state.agent_id

            compositor_app = await registry.get_compositor_app(agent_id)

            response_started = False
            status_code = 200
            headers = []
            body_parts = []

            async def send(message):
                nonlocal response_started, status_code, headers
                if message["type"] == "http.response.start":
                    response_started = True
                    status_code = message["status"]
                    headers = message.get("headers", [])
                elif message["type"] == "http.response.body":
                    body_parts.append(message.get("body", b""))

            await compositor_app(request.scope, request.receive, send)

            response_headers = {k.decode(): v.decode() for k, v in headers}
            body = b"".join(body_parts)
            return Response(content=body, status_code=status_code, headers=response_headers)

    token_mapping = TokenMapping(auth_tokens_path)
    mcp_app = FastAPI(title="MCP Server")

    # Order matters: routing first, then token auth (outermost middleware runs first)
    mcp_app.add_middleware(CompositorRoutingMiddleware)
    mcp_app.add_middleware(TokenAuthMiddleware, token_mapping=token_mapping)

    return mcp_app


async def create_management_ui_app(
    registry: InfrastructureRegistry, ui_token: str | None = None, static_files_dir: Path | None = None
) -> tuple[FastAPI, str]:
    """Create Management UI FastAPI app with global compositor access.

    Returns tuple of (app, ui_token) where ui_token is the Bearer token for authentication.

    The app exposes:
    - Global compositor via streamable HTTP at /mcp (browser MCP client connects here)
    - Basic REST API endpoints (/health, /api/agents)
    - Static files if static_files_dir is provided
    - WebSocket endpoint at /ws/mcp for real-time MCP communication

    Args:
        registry: Infrastructure registry managing all agents
        ui_token: Optional UI token (auto-generated if not provided)
        static_files_dir: Optional directory containing static UI files

    Returns:
        Tuple of (FastAPI app, UI token string)
    """
    from adgn.agent.mcp_bridge.auth import UITokenAuthMiddleware, generate_ui_token
    from adgn.agent.mcp_bridge.compositor_factory import create_global_compositor

    # Generate or use provided UI token
    if ui_token is None:
        ui_token = generate_ui_token()

    app = FastAPI(title="Agent Management UI")

    # Create global compositor (two-level architecture)
    global_compositor = await create_global_compositor(registry)

    # Mount compositor as ASGI app at /mcp for streamable HTTP transport
    # The compositor (FastMCP) is itself an ASGI app
    app.mount("/mcp", global_compositor)

    # Add UI token authentication (applies to all routes except /mcp which has its own auth)
    # Actually, we want auth on /mcp too, so add middleware
    app.add_middleware(UITokenAuthMiddleware, expected_token=ui_token)

    # Health check endpoint
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Basic agents API
    @app.get("/api/agents")
    async def list_agents():
        """List all known agents."""
        agents = []
        for agent_id in registry.known_agents():
            mode = registry.get_agent_mode(agent_id)
            agents.append({"id": agent_id, "mode": mode})
        return {"agents": agents}

    # Static files (if provided)
    if static_files_dir and static_files_dir.exists():
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse

        app.mount("/static", StaticFiles(directory=static_files_dir), name="static")

        @app.get("/")
        async def root():
            return FileResponse(static_files_dir / "index.html")

    return app, ui_token



