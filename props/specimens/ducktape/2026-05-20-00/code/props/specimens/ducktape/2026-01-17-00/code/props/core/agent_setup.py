"""Shared utilities for setting up agent workflows in props evaluation.

Agent environments manage the complete lifecycle for agents that:
- Execute commands via docker_exec in a container
- Access database via scoped temporary credentials
- Call submit tools via MCP-over-HTTP
- Use agent packages (Dockerfile + /init script)

Subclasses configure:
- definition_id: Which agent definition to use
- agent_run_id: UUID for this run (workspace path, RLS scoping)
- MCP server factory (provides agent-specific tools)

The base class handles:
- Definition unpacking to workspace (before container starts)
- Temporary database user lifecycle
- HTTP MCP server startup/shutdown
- Docker container with docker_exec tool
- Init script execution via BootstrapHandler (when using AgentHandle)

Snapshots are fetched by agents themselves at init time via fetch_snapshot() from
props.agent_helpers. No external dependencies at runtime except the database.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from agent_core.handler import BaseHandler
from mcp_infra.compositor.server import Compositor
from mcp_infra.display.rich_display import CompactDisplayHandler
from net_util.docker import get_docker_network_gateway_async
from net_util.net import pick_free_port, wait_for_port
from props.core.agent_workspace import WorkspaceManager
from props.core.cli.common_options import DEFAULT_MAX_LINES
from props.core.db.config import DatabaseConfig
from props.core.db.temp_user_manager import TempUserManager
from props.core.db_event_handler import DatabaseEventHandler
from props.core.docker_env import DOCKER_MOUNT_PREFIX, PROPS_NETWORK_NAME, PropertiesDockerCompositor
from props.core.registry.images import _resolve_image_ref


def _make_container_name(agent_run_id: UUID) -> str:
    """Generate container name from agent run ID only."""
    run_part = str(agent_run_id).split("-")[0]
    return f"agent-{run_part}"


if TYPE_CHECKING:
    import aiodocker
    from fastmcp.server.auth import AuthProvider

logger = logging.getLogger(__name__)


# --- Agent Handlers ---


async def build_props_handlers(
    *, agent_run_id: UUID, verbose_prefix: str | None, compositor: Compositor, max_lines: int = DEFAULT_MAX_LINES
) -> list[BaseHandler]:
    """Build standard handlers for props agent workflows.

    # TODO: Refactor display config threading. Currently `verbose` and `max_lines` are passed
    # separately from CLI through run_critic/run_grader, while `verbose_prefix` is constructed
    # mid-way from internal context (agent_run_id, snapshot_slug, etc.). Consider consolidating
    # into a single `DisplayConfig | None` param constructed at the same level as the prefix,
    # with CLI just passing `max_lines: int | None` (None = no display).

    Always includes DatabaseEventHandler for event persistence.
    Conditionally includes CompactDisplayHandler if verbose_prefix is provided.

    Args:
        agent_run_id: Agent run ID for database event tracking
        verbose_prefix: Optional prefix for verbose display (e.g., "[CRITIC snapshot-slug] ").
                       If None, no verbose handler is added.
        compositor: Compositor instance for extracting server schemas
        max_lines: Max lines per event in verbose display (default from common_options)
    """
    handlers: list[BaseHandler] = [DatabaseEventHandler(agent_run_id=agent_run_id)]

    if verbose_prefix is not None:
        display_handler = await CompactDisplayHandler.from_compositor(
            compositor, max_lines=max_lines, prefix=verbose_prefix
        )
        handlers.append(display_handler)

    return handlers


class AgentEnvironment(ABC):
    """Base class for agent environments with HTTP MCP server.

    Manages complete agent lifecycle:
    1. Pulls OCI image from registry
    2. Creates temporary database user with scoped access
    3. Starts HTTP MCP server with agent-specific tools (via _make_mcp_server)
    4. Creates Docker container with:
       - docker_exec tool available
       - Database credentials in PG* env vars
       - MCP server URL/token in env vars
    5. Cleans up in reverse order on exit

    Snapshots are fetched by agents at init time via fetch_snapshot() from
    props.agent_helpers. No bind mounts for snapshots - agents extract them
    directly from the database.

    Agent image structure (OCI image):
    - /init: Bootstrap script executed before agent sampling (outputs system prompt)
    - /agent.md: Agent-specific prompt portion (optional, used by some /init scripts)

    The /workspace directory is a bind-mounted host directory for runtime files,
    not the agent package contents.

    Subclasses must implement:
    - _make_mcp_server(auth): Create agent-specific MCP server

    Example subclass:
        class CriticAgentEnvironment(AgentEnvironment):
            def __init__(self, docker_client, agent_run_id, db_config, workspace_manager, image):
                super().__init__(
                    agent_run_id=agent_run_id,
                    docker_client=docker_client,
                    db_config=db_config,
                    workspace_manager=workspace_manager,
                    image=image,  # Full OCI ref from AgentRegistry
                )

            def _make_mcp_server(self, auth) -> FastMCP:
                return CriticSubmitServer(...)

    Usage:
        async with CriticAgentEnvironment(...) as compositor:
            # Use AgentHandle.create() to run agent with init script
            handle = await AgentHandle.create(
                agent_run_id=agent_run_id,
                compositor=compositor,
                ...
            )
            await handle.run()
    """

    def __init__(
        self,
        agent_run_id: UUID,
        docker_client: aiodocker.Docker,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        *,
        image: str,
        container_name: str | None = None,
        labels: dict[str, str] | None = None,
        auto_remove: bool = False,
    ):
        self._agent_run_id = agent_run_id
        self._docker_client = docker_client
        self._db_config = db_config
        self._workspace_manager = workspace_manager
        self._image = image  # Full OCI reference (host:port/repo@digest)
        self._container_name = container_name
        self._labels = labels or {}
        self._auto_remove = auto_remove

        self._user_manager: TempUserManager | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._compositor: PropertiesDockerCompositor | None = None
        self._image_id: str | None = None

    @property
    def agent_run_id(self) -> UUID:
        """Agent run ID."""
        return self._agent_run_id

    @property
    def workspace_root(self) -> Path:
        """Path to unpacked definition workspace."""
        return self._workspace_manager.get_path(self._agent_run_id)

    @property
    def workspace_manager(self) -> WorkspaceManager:
        """Workspace manager for this environment."""
        return self._workspace_manager

    @abstractmethod
    def _make_mcp_server(self, auth: AuthProvider) -> FastMCP:
        """Subclasses override to provide agent-specific MCP servers."""

    async def __aenter__(self) -> PropertiesDockerCompositor:
        """Start agent environment: user, HTTP server, container.

        Orchestrates the following order:
        1. Build Docker image from definition archive
        2. Create temporary database user with scoped access
        3. Mount resources/compositor_meta (Compositor base)
        4. Start MCP HTTP server
        5. Set container environment with MCP server URL/token
        6. Create Docker exec server (uses environment)

        Snapshots are not mounted - agents fetch them at init time via
        fetch_snapshot() from props.core.agent_helpers.

        Returns:
            PropertiesDockerCompositor with docker_exec tool available
        """
        # Resolve image from OCI reference
        self._image_id = await _resolve_image_ref(self._docker_client, self._image)
        logger.info(f"Using image {self._image_id[:19]} from {self._image}")

        self._user_manager = TempUserManager(self._db_config.admin, self._agent_run_id)
        temp_creds = await self._user_manager.__aenter__()
        logger.info(f"Created temporary database user: {temp_creds.username}")

        container_db = self._db_config.for_container_user(temp_creds.username, temp_creds.password)

        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        container_host = await get_docker_network_gateway_async(self._docker_client, PROPS_NETWORK_NAME)

        compositor = _AgentDockerCompositor(
            workspace_root=self.workspace_root,
            docker_client=self._docker_client,
            image_id=self._image_id,
            db_conn=container_db,
            agent_run_id=self._agent_run_id,
            container_name=self._container_name,
            labels=self._labels,
            auto_remove=self._auto_remove,
        )
        self._compositor = compositor

        await Compositor.__aenter__(compositor)

        token = secrets.token_hex(32)
        auth = StaticTokenVerifier({token: {"client_id": "mcp_agent", "scopes": []}})
        port = pick_free_port(host="127.0.0.1")
        server = self._make_mcp_server(auth)
        app = server.http_app(transport="streamable-http")
        config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
        uv_server = uvicorn.Server(config)
        server_task = asyncio.create_task(uv_server.serve())

        async def _shutdown_http_server():
            uv_server.should_exit = True
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except TimeoutError:
                logger.warning("Server shutdown timed out, cancelling")
                server_task.cancel()
                with suppress(asyncio.CancelledError):
                    await server_task
            except asyncio.CancelledError:
                pass
            logger.info(f"MCP HTTP server on port {port} shut down")

        self._exit_stack.push_async_callback(lambda: _shutdown_http_server())

        await asyncio.to_thread(wait_for_port, "127.0.0.1", port, timeout_secs=10.0)
        url = f"http://{container_host}:{port}/mcp"
        logger.info(f"MCP HTTP server started at {url}")

        compositor._extra_env = {"MCP_SERVER_URL": url, "MCP_SERVER_TOKEN": token}

        docker_server = compositor._create_docker_server(self._image_id)
        compositor.runtime = await compositor.mount_inproc(DOCKER_MOUNT_PREFIX, docker_server, pinned=True)

        logger.info("Started agent environment")

        return compositor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up agent environment: HTTP server, compositor, user (reverse order)."""
        logger.info("AgentEnvironment.__aexit__: starting cleanup")

        try:
            if self._exit_stack:
                logger.info("AgentEnvironment.__aexit__: cleaning up HTTP server")
                await self._exit_stack.__aexit__(exc_type, exc_val, exc_tb)
                self._exit_stack = None

            if self._compositor is not None:
                logger.info("AgentEnvironment.__aexit__: cleaning up compositor")
                await Compositor.__aexit__(self._compositor, exc_type, exc_val, exc_tb)
                self._compositor = None

        finally:
            if self._user_manager is not None:
                logger.info("AgentEnvironment.__aexit__: cleaning up temp user")
                await self._user_manager.__aexit__(exc_type, exc_val, exc_tb)
                self._user_manager = None

        logger.info("AgentEnvironment.__aexit__: cleanup complete")


class _AgentDockerCompositor(PropertiesDockerCompositor):
    """Internal compositor used by AgentEnvironment.

    This is a simplified version that doesn't run its own __aenter__/__aexit__.
    AgentEnvironment manually orchestrates the lifecycle to inject HTTP server setup
    before Docker server creation.
    """

    def __init__(
        self,
        workspace_root: Path,
        docker_client: aiodocker.Docker,
        image_id: str,
        db_conn,
        *,
        agent_run_id: UUID,
        container_name: str | None,
        labels: dict[str, str],
        auto_remove: bool,
    ):
        merged_labels = {"adgn.project": "props", "adgn.agent_run_id": str(agent_run_id), **labels}
        name = container_name or _make_container_name(agent_run_id)
        super().__init__(
            workspace_root,
            docker_client,
            image_id=image_id,
            db_conn=db_conn,
            workspace_mode="rw",
            network_mode=PROPS_NETWORK_NAME,  # Must allow container→host communication
            extra_env=None,  # Will be set by AgentEnvironment after HTTP server starts
            labels=merged_labels,
            container_name=name,
            auto_remove=auto_remove,
        )
