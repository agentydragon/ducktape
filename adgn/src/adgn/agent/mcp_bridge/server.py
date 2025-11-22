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
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from adgn.agent.mcp_bridge.auth import TokenAuthMiddleware, TokenMapping, UITokenAuthMiddleware, generate_ui_token
from adgn.agent.mcp_bridge.compositor_factory import (
    create_global_compositor,
    mount_agent_compositor_dynamically,
    unmount_agent_compositor_dynamically,
)
from adgn.agent.mcp_bridge.types import AgentID, AgentMode, RunPhase
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
    local_runtime: LocalAgentRuntime | None  # None for bridge agents

    @property
    def mode(self) -> AgentMode:
        """Derive mode from local_runtime presence."""
        return AgentMode.LOCAL if self.local_runtime else AgentMode.BRIDGE


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
    return await MCPInfrastructure(
        agent_id=agent_id,
        persistence=persistence,
        docker_client=docker_client,
        initial_policy=initial_policy
    ).start(mcp_config)


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
                running=running, compositor_app=compositor_app, local_runtime=None
            )

            logger.info(f"Infrastructure ready for {agent_id=}")
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

    def _get_agent_or_raise(self, agent_id: AgentID) -> RunningAgent:
        """Get RunningAgent or raise KeyError if not found/initialized."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found in registry")
        if (agent := self._agents[agent_id].agent) is None:
            raise KeyError(f"Agent {agent_id} not yet initialized")
        return agent

    async def get_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure:
        """Get infrastructure. Raises KeyError if not found."""
        return self._get_agent_or_raise(agent_id).running

    def get_agent_mode(self, agent_id: AgentID) -> AgentMode:
        """Get agent mode. Raises KeyError if not found."""
        return self._get_agent_or_raise(agent_id).mode

    def get_local_runtime(self, agent_id: AgentID) -> LocalAgentRuntime | None:
        """Get local runtime or None if bridge agent. Raises KeyError if not found."""
        return self._get_agent_or_raise(agent_id).local_runtime

    def register_local_agent(
        self,
        agent_id: AgentID,
        running: RunningInfrastructure,
        compositor_app: FastAPI,
        local_runtime: LocalAgentRuntime,
    ) -> None:
        self._agents[agent_id].agent = RunningAgent(
            running=running, compositor_app=compositor_app, local_runtime=local_runtime
        )

    async def create_agent(self, agent_id: AgentID) -> RunningInfrastructure:
        """Create and initialize infrastructure for a new agent.

        Returns the running infrastructure for the agent.
        """
        running, _ = await self.get_or_create_infrastructure(agent_id)
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
        """Remove and clean up agent infrastructure."""
        agent = self._get_agent_or_raise(agent_id)
        await agent.running.close()
        del self._agents[agent_id]
        await self.notify_agents_list_changed()

    def _determine_run_phase(
        self, infra: RunningInfrastructure | None
    ) -> tuple[RunPhase, int]:
        """Determine run phase and pending approvals count."""
        if not infra:
            return RunPhase.IDLE, 0

        pending_approvals = len(infra.approval_hub.pending)
        if pending_approvals > 0:
            return RunPhase.WAITING_APPROVAL, pending_approvals
        else:
            return RunPhase.SAMPLING, pending_approvals

    def _register_resources(self) -> None:
        @self.resource("resource://agents/list", name="agents_list", mime_type="application/json")
        async def list_agents() -> AgentsListResponse:
            """List all agents with detailed status."""
            agents = []
            for agent_id, entry in self._agents.items():
                if entry.agent is None:
                    continue  # Skip uninitialized agents

                agent = entry.agent

                # Get infrastructure if available
                infra = agent.running
                live = infra is not None

                # Determine run phase and pending approvals
                run_phase, pending_approvals = self._determine_run_phase(infra)

                # Determine capabilities
                is_local = agent.mode == AgentMode.LOCAL

                agents.append(
                    AgentInfo(
                        id=agent_id,
                        mode=agent.mode,
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
            agent = self._get_agent_or_raise(agent_id)

            infra = agent.running
            live = infra is not None

            # Determine run phase and pending approvals
            run_phase, pending_approvals = self._determine_run_phase(infra)

            is_local = agent.mode == AgentMode.LOCAL

            return AgentInfo(
                id=agent_id,
                mode=agent.mode,
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

            if self._global_compositor is not None:

                await mount_agent_compositor_dynamically(
                    global_compositor=self._global_compositor,
                    agent_id=agent_id,
                    registry=self
                )

            await self.notify_agents_list_changed()

            return {"agent_id": agent_id, "status": "created"}

        @self.tool()
        async def delete_agent(agent_id: AgentID) -> dict:
            """Delete an agent and unmount its compositor.

            Returns:
                Dictionary with agent_id and status
            """
            if self._global_compositor is not None:

                await unmount_agent_compositor_dynamically(
                    global_compositor=self._global_compositor,
                    agent_id=agent_id
                )

            await self.remove_agent(agent_id)

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

    if ui_token is None:
        ui_token = generate_ui_token()

    app = FastAPI(title="Agent Management UI")

    # Create global compositor (two-level architecture)
    global_compositor = await create_global_compositor(registry)

    # Mount compositor as ASGI app at /mcp for streamable HTTP transport
    app.mount("/mcp", global_compositor)

    app.add_middleware(UITokenAuthMiddleware, expected_token=ui_token)

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

        app.mount("/static", StaticFiles(directory=static_files_dir), name="static")

        @app.get("/")
        async def root():
            return FileResponse(static_files_dir / "index.html")

    return app, ui_token



