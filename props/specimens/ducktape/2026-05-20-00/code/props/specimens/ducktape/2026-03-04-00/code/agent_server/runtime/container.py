from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import aiodocker
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from fastmcp.mcp_config import MCPConfig
from pydantic import BaseModel, Field

from agent_core.agent import Agent
from agent_core.loop_control import RequireAnyTool
from agent_core.mcp_provider import MCPToolProvider
from agent_server.agent_types import AgentID
from agent_server.approvals import load_default_policy_source
from agent_server.mcp.approval_policy.engine import PolicyEngine
from agent_server.mcp.chat.server import attach_persisted_chat_servers
from agent_server.mcp.loop.server import LoopServer
from agent_server.mcp.runtime.server import RuntimeServer
from agent_server.mcp.ui.server import UiServer
from agent_server.persist.handler import RunPersistenceHandler
from agent_server.persist.sqlite import SQLitePersistence
from agent_server.persist.types import ApprovalOutcome
from agent_server.presets import discover_presets
from agent_server.runtime.handlers import build_handlers
from agent_server.runtime.images import resolve_runtime_image
from agent_server.server.bus import ServerBus
from agent_server.server.runtime import AgentSession, UiEventHandler
from agent_server.server.system_message import get_ui_system_message
from mcp_infra.compositor.clients import CompositorMetaClient
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.compositor.notifications_buffer import NotificationsBuffer
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.docker.container_session import ContainerOptions
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.snapshots import SamplingSnapshot, ServerEntry
from openai_utils.client_factory import build_client
from openai_utils.model import OpenAIModelProto, SystemMessage

if TYPE_CHECKING:
    from agent_server.mcp.approval_policy.engine import PolicyProposerServer, PolicyReaderServer
    from mcp_infra.mounted import Mounted

# ---- Agent Container Compositor ---------------------------------------------


class AgentContainerCompositor(Compositor):
    """Compositor for agent containers with all agent-specific servers pre-mounted.

    Infrastructure servers (resources, compositor_meta) are auto-mounted by base Compositor.
    This class adds 6 agent-specific servers:
    - loop: Loop control (pinned)
    - policy_reader: Policy reading (pinned)
    - policy_proposer: Policy proposals (pinned)
    - compositor_admin: Compositor admin tools (pinned)
    - ui: UI server (conditional, pinned if present)
    - runtime: Docker exec runtime (conditional, pinned if present)
    """

    # Agent-specific servers (5 additional beyond base infrastructure)
    loop: Mounted[LoopServer]
    policy_reader: Mounted[PolicyReaderServer]
    policy_proposer: Mounted[PolicyProposerServer]
    ui: Mounted[UiServer] | None
    runtime: Mounted[RuntimeServer] | None

    def __init__(
        self,
        approval_engine: PolicyEngine,
        ui_bus: ServerBus | None,
        async_docker_client: aiodocker.Docker,
        persistence: SQLitePersistence,
        agent_id: AgentID,
    ):
        super().__init__()
        self._approval_engine = approval_engine
        self._ui_bus = ui_bus
        self._async_docker_client = async_docker_client
        self._persistence = persistence
        self._agent_id = agent_id

    async def __aenter__(self):
        # Call base Compositor.__aenter__ (mounts resources + compositor_meta)
        await super().__aenter__()

        # Mount agent-specific servers (all pinned)

        # Loop control (agent-only surface)
        self.loop = await self.mount_inproc(MCPMountPrefix("loop"), LoopServer(), pinned=True)

        # Policy servers (from approval engine)
        self.policy_reader = await self.mount_inproc(
            MCPMountPrefix("policy_reader"), self._approval_engine.reader, pinned=True
        )

        self.policy_proposer = await self.mount_inproc(
            MCPMountPrefix("policy_proposer"), self._approval_engine.proposer, pinned=True
        )

        # Conditionally mount UI and runtime (iff ui_bus is not None)
        if self._ui_bus is not None:
            # UI server
            self.ui = await self.mount_inproc(MCPMountPrefix("ui"), UiServer(self._ui_bus), pinned=True)

            # Runtime exec server
            runtime_image = resolve_runtime_image()
            preset_label = None
            if self._persistence is not None:
                row = await self._persistence.get_agent(self._agent_id)
                if row and row.metadata is not None:
                    preset_label = row.metadata.preset
            opts = ContainerOptions(
                image=runtime_image,
                binds=None,
                labels={
                    "agent_server.project": "agent-runtime",
                    "agent_server.role": "runtime",
                    "agent_server.agent_id": str(self._agent_id),
                    **({"agent_server.preset": preset_label} if preset_label else {}),
                },
            )
            self.runtime = await self.mount_inproc(
                MCPMountPrefix("runtime"), RuntimeServer(self._async_docker_client, opts), pinned=True
            )

            # Attach persisted chat servers
            await attach_persisted_chat_servers(self, persistence=self._persistence, agent_id=self._agent_id)
        else:
            self.ui = None
            self.runtime = None

        return self


# ---- Typed actor messages/results -------------------------------------------


@dataclass
class CloseResult:
    drained: bool
    error: str | None = None


type ActorResult = SamplingSnapshot | CloseResult | None


# ---- Agent control MCP models ------------------------------------------------


class SendPromptInput(BaseModel):
    """Input for the send_prompt tool."""

    prompt: str = Field(description="The prompt to send to start an agent run")


class _ActorMsg:
    pass


@dataclass
class _StartMsg(_ActorMsg):
    """Start the container with the provided MCPConfig (typed FastMCP config)."""

    mcp_config: MCPConfig


class _SamplingSnapshotMsg(_ActorMsg):
    """Request a one-shot sampling snapshot for UI/model consumption."""


class _CloseMsg(_ActorMsg):
    """Request container shutdown; drains, closes agent and mcp manager."""


logger = logging.getLogger(__name__)


def default_client_factory(model: str) -> OpenAIModelProto:
    return build_client(model, enable_debug_logging=True)


@dataclass
class UiFacet:
    manager: UiEventHandler
    ui_bus: ServerBus


@dataclass
class AgentContainer:
    """Actor-owned container that manages MCP + Agent lifecycles in a single task.

    After start, the following fields are populated: mcp, session, agent, persist_handler, ui, approval_engine.
    The approval hub is constructed at init time.

    TODO: This is a god object with too many responsibilities (MCP infrastructure,
    policy management, agent runtime, UI integration, persistence, actor lifecycle).
    Should be refactored into focused components (MCPInfrastructure, PolicyInfrastructure,
    AgentRuntime) with clear separation of concerns. The 150+ line initialization in
    _handle_actor_msg should be broken into smaller initialization functions.
    """

    agent_id: AgentID
    persistence: SQLitePersistence
    model: str
    client_factory: Callable[[str], OpenAIModelProto]
    async_docker_client: aiodocker.Docker
    with_ui: bool = True

    # Populated after Start
    approval_engine: PolicyEngine | None = None
    session: AgentSession | None = None
    agent: Agent | None = None
    persist_handler: RunPersistenceHandler | None = None
    ui: UiFacet | None = None
    # Optional system prompt override (e.g., from preset)
    system_override: str | None = None
    # Bound docker client for volume IO (injected by registry/app)
    # Optional initial policy to apply on first start (creation only)
    initial_policy: str | None = None

    # Actor internals
    _mailbox: asyncio.Queue[tuple[_ActorMsg, asyncio.Future[ActorResult]]] = field(
        default_factory=asyncio.Queue, init=False
    )
    _actor_task: asyncio.Task | None = field(default=None, init=False)
    _ready: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _closed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
    # Internal helpers/state
    _cm: UiEventHandler | None = field(default=None, init=False)
    _ui_bus: ServerBus | None = field(default=None, init=False)
    # Compositor instance (AgentContainerCompositor with all agent-specific servers)
    _compositor: AgentContainerCompositor | None = field(default=None, init=False)
    # Front-door MCP client (FastMCP Client connected to the compositor with policy middleware)
    _compositor_client: Client | None = field(default=None, init=False)

    @property
    def compositor_client(self) -> Client | None:
        """Front-door MCP client connected to the compositor (if started)."""
        return self._compositor_client

    @property
    def runtime_server(self):
        """Get the runtime server instance (if compositor is started and runtime is mounted)."""

        if self._compositor is None or self._compositor.runtime is None:
            return None
        return self._compositor.runtime.server

    async def list_mcp_entries(self) -> dict[str, ServerEntry]:
        """Return full per-server entries via compositor_meta resources.

        Empty when MCP is not initialized.
        """
        if self._compositor_client is None:
            return {}
        meta = CompositorMetaClient(self._compositor_client)
        return cast(dict[str, ServerEntry], await meta.list_states())

    def _get_session(self) -> AgentSession:
        """Get the agent session, raising ToolError if not initialized."""
        if self.session is None:
            raise ToolError("Agent session not initialized")
        return self.session

    def make_control_server(self) -> EnhancedFastMCP:
        """Create an agent control MCP server for this container.

        Tools:
        - send_prompt(prompt) - Send a prompt to the agent
        - abort() - Abort the currently active agent

        Returns:
            EnhancedFastMCP server instance
        """
        mcp = EnhancedFastMCP("Agent Control MCP Server")
        container = self  # capture self for closures

        @mcp.flat_model()
        async def send_prompt(input: SendPromptInput) -> str:
            """Send a prompt to the agent."""
            session = container._get_session()
            await session.run(input.prompt)
            return "Prompt sent successfully"

        @mcp.flat_model()
        async def abort() -> str:
            """Abort the currently active agent."""
            session = container._get_session()
            await session.cancel_active_run()
            return "Agent aborted successfully"

        return mcp

    # ---- Phase-based initialization methods ---------------------------------

    async def _setup_approval_infrastructure(self) -> PolicyEngine:
        """Phase 1: Set up approval infrastructure.

        Resolves the initial policy source (from preset, initial_policy parameter, or default)
        and constructs the PolicyEngine which owns servers, hub, and gateway.

        Returns:
            PolicyEngine: Complete policy subsystem
        """
        # Resolve initial policy source via preset/persistence/override
        row = await self.persistence.get_agent(self.agent_id)
        preset_name: str | None = None
        if row and row.metadata is not None:
            preset_name = row.metadata.preset

        # Always discover presets (cheap operation, loads metadata only)
        presets = discover_presets(override_dir=os.getenv("ADGN_AGENT_PRESETS_DIR"))
        preset = presets.get(preset_name) if preset_name else None
        chosen = (
            self.initial_policy
            or (preset.approval_policy if (preset and preset.approval_policy) else None)
            or load_default_policy_source()
        )

        # Construct PolicyEngine - owns servers, hub, and gateway
        return PolicyEngine(
            agent_id=self.agent_id,
            persistence=self.persistence,
            policy_source=chosen,
            docker_client=self.async_docker_client,
        )

    async def _setup_mcp_infrastructure(
        self, approval_engine: PolicyEngine, mcp_config: MCPConfig
    ) -> tuple[AgentContainerCompositor, Client, NotificationsBuffer]:
        """Phase 2: Set up MCP infrastructure.

        Creates the AgentContainerCompositor with all agent-specific servers,
        mounts external servers from config, sets up notifications buffer,
        creates MCP client, and installs policy gateway middleware.

        Args:
            approval_engine: The PolicyEngine from phase 1 (owns gateway)
            mcp_config: MCP configuration with servers to mount

        Returns:
            tuple: (compositor, mcp_client, notifications_buffer)
        """
        # Session & manager
        self._cm = UiEventHandler()

        # Initialize AsyncExitStack and enter contexts through it
        await self._stack.__aenter__()

        # Create AgentContainerCompositor with all agent-specific servers
        # This will auto-mount: loop, policy_reader, policy_proposer, ui (if present), runtime (if present),
        # plus infrastructure servers (resources, compositor_meta) from base Compositor
        compositor = AgentContainerCompositor(
            approval_engine=approval_engine,
            ui_bus=self._ui_bus,
            async_docker_client=self.async_docker_client,
            persistence=self.persistence,
            agent_id=self.agent_id,
        )
        self._compositor = await self._stack.enter_async_context(compositor)
        assert self._compositor is not None  # Type narrowing for mypy

        # Mount external servers from config (compositor is in ACTIVE state)
        await self._compositor.mount_servers_from_config(mcp_config)

        # Notifications buffer for MCP events
        notif_buffer = NotificationsBuffer(compositor=self._compositor)

        # In-proc client to the compositor with policy middleware
        mcp_client = Client(self._compositor, message_handler=notif_buffer.handler)
        await self._stack.enter_async_context(mcp_client)
        self._compositor_client = mcp_client
        self._notif_buffer = notif_buffer

        # Install policy gateway middleware from engine
        self._compositor.add_middleware(approval_engine.gateway)

        return (self._compositor, mcp_client, notif_buffer)

    async def _setup_agent_runtime(
        self, mcp_client: Client, notifications: NotificationsBuffer, approval_engine: PolicyEngine
    ) -> tuple[AgentSession, Agent]:
        """Phase 3: Set up agent runtime.

        Creates the agent session, builds message handlers, creates the agent,
        and wires everything together.

        Args:
            mcp_client: MCP client from phase 2
            notifications: Notifications buffer from phase 2
            approval_engine: PolicyEngine from phase 1 (owns hub and gateway)

        Returns:
            tuple: (session, agent)
        """
        if self._cm is None:
            raise RuntimeError("connection manager not initialized")
        manager = self._cm

        # Create session
        sess = AgentSession(
            manager,
            persistence=self.persistence,
            agent_id=self.agent_id,
            approval_engine=approval_engine,
            ui_bus=self._ui_bus if self.with_ui else None,
            ui_mount=self._compositor.ui if self.with_ui and self._compositor else None,
        )

        # LLM client
        client = self.client_factory(self.model)

        # Build handlers
        handlers, persist_handler = build_handlers(
            poll_notifications=notifications.poll,
            manager=manager,
            persistence=self.persistence,
            agent_id=self.agent_id,
            compositor=self._compositor,
            ui_bus=self._ui_bus if self.with_ui else None,
        )
        sess.set_persist_handler(persist_handler)

        # Compose base system text and provide a dynamic provider that recomputes
        # grouped MCP instructions/capabilities on each sampling.
        base_system = self.system_override or str(get_ui_system_message())
        assert self._compositor is not None

        # Start agent
        agent = await Agent.create(
            tool_provider=MCPToolProvider(mcp_client),
            client=client,
            handlers=handlers,
            dynamic_instructions=self._compositor.render_agent_dynamic_instructions,
            tool_policy=RequireAnyTool(),
        )
        agent.process_message(SystemMessage.text(base_system))
        # Note: Agent doesn't own resources, no cleanup needed

        # Session tracks the system used for persisted run metadata; store base system
        sess.attach_agent(agent, model=self.model, system=base_system)

        # Create UI facet if needed
        if self.with_ui and self._ui_bus is not None:
            self.ui = UiFacet(manager=manager, ui_bus=self._ui_bus)

        # Store persist handler
        self.persist_handler = persist_handler

        return (sess, agent)

    # ---- Actor dispatch -----------------------------------------------------

    async def _handle_actor_msg(self, msg: _ActorMsg) -> ActorResult:
        """Dispatch a typed actor message using structural pattern matching."""
        match msg:
            case _StartMsg(mcp_config=mcp_cfg):
                # Phase 1: PolicyEngine (owns servers, hub, gateway)
                self.approval_engine = await self._setup_approval_infrastructure()

                # Phase 2: MCP infrastructure
                (self._compositor, self._compositor_client, self._notif_buffer) = await self._setup_mcp_infrastructure(
                    self.approval_engine, mcp_cfg
                )

                # Phase 3: Agent runtime
                self.session, self.agent = await self._setup_agent_runtime(
                    self._compositor_client, self._notif_buffer, self.approval_engine
                )

                return None
            case _SamplingSnapshotMsg():
                if self._compositor is None:
                    return None
                return await self._compositor.sampling_snapshot()
            case _CloseMsg():
                return await self._op_close()
            case _:
                raise TypeError(f"unsupported actor message: {type(msg).__name__}")

    def _ensure_actor(self) -> None:
        if self._actor_task is None:
            self._actor_task = asyncio.create_task(self._actor_loop())

    async def _post_msg(self, msg: _ActorMsg) -> ActorResult:
        fut: asyncio.Future[ActorResult] = asyncio.get_running_loop().create_future()
        await self._mailbox.put((msg, fut))
        return await fut

    async def start(self, *, mcp_config: MCPConfig) -> None:
        self._ensure_actor()
        await self._post_msg(_StartMsg(mcp_config=mcp_config))
        await self._ready.wait()

    async def close(self) -> CloseResult:
        if self._actor_task is None:
            return CloseResult(drained=True)
        result = await self._post_msg(_CloseMsg())
        await self._closed.wait()
        assert isinstance(result, CloseResult)
        return result

    async def sampling_snapshot(self) -> SamplingSnapshot | None:
        """Return a structured snapshot of servers/tools via the actor."""
        res = await self._post_msg(_SamplingSnapshotMsg())
        return cast(SamplingSnapshot | None, res)

    async def record_policy_outcome(self, call_id: str, tool_key: str, outcome: ApprovalOutcome) -> None:
        await self.persistence.record_approval(
            agent_id=self.agent_id, call_id=call_id, tool_key=tool_key, outcome=outcome, decided_at=datetime.now(UTC)
        )

    async def _op_close(self) -> CloseResult:
        drained_ok = True
        drain_error: Exception | None = None
        try:
            if self.ui:
                await self.ui.manager.flush()
            if self.session is not None:
                await self.session.cancel_active_run()
            # No manager-level idle wait; composited client calls have finished
            if self.persist_handler is not None:
                await self.persist_handler.drain()
        except Exception as e:
            drained_ok = False
            drain_error = e
        finally:
            try:
                await self._stack.aclose()
            finally:
                self._closed.set()
        return CloseResult(
            drained=drained_ok,
            error=type(drain_error).__name__ if (not drained_ok and drain_error is not None) else None,
        )

    async def _actor_loop(self) -> None:
        try:
            while True:
                msg, fut = await self._mailbox.get()
                try:
                    result = await self._handle_actor_msg(msg)
                    fut.set_result(result)
                    if isinstance(msg, _CloseMsg):
                        break
                except Exception as e:  # deliver failure back to caller
                    if not fut.done():
                        fut.set_exception(e)
        except Exception as e:
            # Unhandled actor failure; log and signal closed so registry can clean up
            logger.exception("container actor crashed", exc_info=e)
            self._closed.set()


async def build_container(
    *,
    agent_id: AgentID,
    mcp_config: MCPConfig,
    persistence: SQLitePersistence,
    model: str,
    client_factory: Callable[[str], OpenAIModelProto],
    with_ui: bool = True,
    system: str | None = None,
    async_docker_client: aiodocker.Docker,
    initial_policy: str | None = None,
) -> AgentContainer:
    c = AgentContainer(
        agent_id=agent_id,
        persistence=persistence,
        model=model,
        client_factory=client_factory,
        with_ui=with_ui,
        async_docker_client=async_docker_client,
        system_override=system,
        initial_policy=initial_policy,
    )
    await c.start(mcp_config=mcp_config)
    return c
