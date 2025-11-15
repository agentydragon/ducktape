from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import os
from typing import TypeAlias, cast

from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig, MCPServerTypes

from adgn.agent.agent import MiniCodex
from adgn.agent.approvals import (
    ApprovalHub,
    ApprovalPolicyEngine,
    load_default_policy_source,
    make_policy_engine,
)
from adgn.agent.persist import ApprovalOutcome
from adgn.agent.persist.handler import RunPersistenceHandler
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.presets import discover_presets
from adgn.agent.runtime.images import resolve_runtime_image
from adgn.agent.server.bus import ServerBus
from adgn.agent.server.protocol import ApprovalPendingEvt
from adgn.agent.server.rendering import render_compositor_instructions
from adgn.agent.server.runtime import AgentSession, ConnectionManager
from adgn.agent.server.system_message import get_ui_system_message
from adgn.mcp._shared.constants import (
    APPROVAL_POLICY_SERVER_NAME_APPROVER,
    APPROVAL_POLICY_SERVER_NAME_PROPOSER,
    APPROVAL_POLICY_SERVER_NAME_READER,
    RUNTIME_EXEC_TOOL_NAME,
    RUNTIME_SERVER_NAME,
    SEATBELT_EXEC_SERVER_NAME,
    UI_SERVER_NAME,
)
from adgn.mcp._shared.container_session import ContainerOptions
from adgn.mcp.approval_policy.clients import PolicyApproverClient, PolicyReaderClient
from adgn.mcp.approval_policy.server import (
    ApprovalPolicyAdminServer,
    ApprovalPolicyProposerServer,
    ApprovalPolicyServer,
)
from adgn.mcp.chat.server import attach_persisted_chat_servers
from adgn.mcp.compositor.clients import CompositorAdminClient, CompositorMetaClient
from adgn.mcp.compositor.server import Compositor
from adgn.mcp.compositor.setup import mount_standard_inproc_servers

# in-proc embed is used; no external HTTP readiness needed
from adgn.mcp.exec.seatbelt import attach_seatbelt_exec
from adgn.mcp.loop.server import make_loop_server
from adgn.mcp.notifications.buffer import NotificationsBuffer
from adgn.mcp.policy_gateway.middleware import install_policy_gateway
from adgn.mcp.runtime.server import make_runtime_server
from adgn.mcp.snapshots import SamplingSnapshot, ServerEntry
from adgn.mcp.ui.server import make_ui_server
from adgn.openai_utils.client_factory import build_client
from adgn.openai_utils.model import OpenAIModelProto
from docker import DockerClient

from .handlers import build_handlers

# ---- Typed actor messages/results -------------------------------------------


@dataclass
class CloseResult:
    drained: bool
    error: str | None = None


ActorResult: TypeAlias = SamplingSnapshot | CloseResult | None


class _ActorMsg:
    pass


@dataclass
class _StartMsg(_ActorMsg):
    """Start the container with the provided MCPConfig (typed FastMCP config)."""

    mcp_config: MCPConfig


@dataclass
class _ReconfigureMsg(_ActorMsg):
    """Apply live changes to the mounted servers.

    - mcp_config: when provided, full replacement of mounts.
    - attach: map of arbitrary names to MCPConfig fragments; each config's servers are merged in.
    - detach: list of server names to unmount.
    """

    mcp_config: MCPConfig | None
    attach: dict[str, MCPConfig]
    detach: list[str]


class _SamplingSnapshotMsg(_ActorMsg):
    """Request a one-shot sampling snapshot for UI/model consumption."""

    pass


class _SamplingSnapshotIncrementalMsg(_ActorMsg):
    """Request an incremental sampling snapshot stream as servers initialize."""

    pass


class _CloseMsg(_ActorMsg):
    """Request container shutdown; drains, closes agent and mcp manager."""

    pass


@dataclass
class _AttachOneMsg(_ActorMsg):
    """Attach a single MCP server by name with a typed fastmcp spec."""

    name: str
    spec: MCPServerTypes


@dataclass
class _DetachOneMsg(_ActorMsg):
    """Detach a single MCP server by name."""

    name: str


logger = logging.getLogger(__name__)


def default_client_factory(model: str) -> OpenAIModelProto:
    """Default LLM client factory used when no custom factory is provided."""

    return build_client(model, enable_debug_logging=True)


@dataclass
class UiFacet:
    manager: ConnectionManager
    ui_bus: ServerBus


@dataclass
class AgentContainer:
    """Actor-owned container that manages MCP + Agent lifecycles in a single task.

    After start, the following fields are populated: mcp, session, agent, persist_handler, ui, approval_engine.
    The approval hub is constructed at init time.
    """

    agent_id: str
    persistence: SQLitePersistence
    model: str
    client_factory: Callable[[str], OpenAIModelProto]
    docker_client: DockerClient
    with_ui: bool = True
    # Runtime exec server characteristics (wired during attach)
    # Default: runtime is not treated as ephemeral for status purposes
    runtime_ephemeral: bool = False

    # Populated after Start
    approval_engine: ApprovalPolicyEngine | None = None
    approval_hub: ApprovalHub = field(default_factory=ApprovalHub)
    session: AgentSession | None = None
    agent: MiniCodex | None = None
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
    _cm: ConnectionManager | None = field(default=None, init=False)
    _ui_bus: ServerBus | None = field(default=None, init=False)
    # Compositor instance (when compositor path is used)
    _compositor: Compositor | None = field(default=None, init=False)
    # Front-door MCP client (FastMCP Client connected to the compositor with policy middleware)
    _mcp_client: Client | None = field(default=None, init=False)
    # No direct reference to resources server; mounted via helper
    # Policy clients (managed via AsyncExitStack)
    _policy_reader: PolicyReaderClient | None = field(default=None, init=False)
    _policy_approver: PolicyApproverClient | None = field(default=None, init=False)

    @property
    def policy_approver(self) -> PolicyApproverClient:
        if self._policy_approver is None:
            raise RuntimeError("policy approver client not initialized")
        return self._policy_approver

    @property
    def mcp_client(self) -> Client | None:
        """Front-door MCP client connected to the compositor (if started)."""
        return self._mcp_client

    async def list_mcp_entries(self) -> dict[str, ServerEntry]:
        """Return full per-server entries via compositor_meta resources.

        Empty when MCP is not initialized.
        """
        if self._mcp_client is None:
            return {}
        meta = CompositorMetaClient(self._mcp_client)
        return await meta.list_states()

    # ---- Actor dispatch -----------------------------------------------------

    async def _handle_actor_msg(self, msg: _ActorMsg) -> ActorResult:
        """Dispatch a typed actor message using structural pattern matching."""
        match msg:
            case _StartMsg(mcp_config=mcp_cfg):
                # Inline former _op_start
                # Resolve initial policy source via preset/persistence/override
                row = await self.persistence.get_agent(self.agent_id)
                preset_name: str | None = None
                if row and row.metadata is not None:
                    preset_name = row.metadata.preset
                presets = (
                    discover_presets(os.getenv("ADGN_AGENT_PRESETS_DIR")) if preset_name else {}
                )
                preset = presets.get(preset_name) if preset_name else None
                chosen = (
                    self.initial_policy
                    or (preset.approval_policy if (preset and preset.approval_policy) else None)
                    or load_default_policy_source()
                )

                # Construct the approval engine with the chosen initial policy (DI)
                self.approval_engine = make_policy_engine(
                    agent_id=self.agent_id,
                    persistence=self.persistence,
                    docker_client=self.docker_client,
                    policy_source=chosen,
                )
                # approval_hub is constructed at init; ensure it exists
                assert self.approval_hub is not None
                # Policy helper resolution happens via handler on-demand

                # Session & manager
                self._cm = ConnectionManager()
                sess = AgentSession(
                    self._cm,
                    approval_hub=self.approval_hub,
                    persistence=self.persistence,
                )
                sess.agent_id = self.agent_id
                if self.with_ui and self._ui_bus is not None:
                    sess.ui_bus = self._ui_bus
                sess.approval_engine = self.approval_engine

                # LLM client
                client = self.client_factory(self.model)
                # Initialize AsyncExitStack and enter contexts through it
                await self._stack.__aenter__()
                # In-proc Compositor (embedded)
                comp = Compositor("compositor", eager_open=True)
                for name, server_cfg in mcp_cfg.mcpServers.items():
                    await comp.mount_server(name, server_cfg)
                # Mount loop control server (agent-only surface)
                loop_server = make_loop_server("loop")
                await comp.mount_inproc("loop", loop_server)
                self._compositor = comp
                # Notifications buffer for MCP events
                notif_buffer = NotificationsBuffer(compositor=comp)
                # In-proc client to the compositor with policy middleware
                mcp_client = Client(comp, message_handler=notif_buffer.handler)
                await self._stack.enter_async_context(mcp_client)
                self._mcp_client = mcp_client
                self._notif_buffer = notif_buffer
                # Coalesced Snapshot refresh
                self._snapshot_push_pending = False

                async def _coalesced_push() -> None:
                    if self._snapshot_push_pending:
                        return
                    self._snapshot_push_pending = True
                    try:
                        await asyncio.sleep(0.05)
                        await self._push_snapshot_and_status()
                    finally:
                        self._snapshot_push_pending = False

                notif_buffer.add_hook(lambda: asyncio.create_task(_coalesced_push()))
                # Attach in-proc UI/approval/runtime servers
                await self._attach_inproc_servers(self._ui_bus)

                def _get_run_id():
                    return sess.active_run.run_id if sess.active_run else None

                handlers, persist_handler = build_handlers(
                    poll_notifications=notif_buffer.poll,
                    manager=self._cm,
                    persistence=self.persistence,
                    approval_engine=self.approval_engine,
                    approval_hub=self.approval_hub,
                    get_run_id=_get_run_id,
                    agent_id=self.agent_id,
                    ui_bus=self._ui_bus if self.with_ui else None,
                )
                sess.set_persist_handler(persist_handler)

                # Compose base system text and provide a dynamic provider that recomputes
                # grouped MCP instructions/capabilities on each sampling.
                base_system = self.system_override or str(get_ui_system_message())
                assert self._compositor is not None

                async def _dynamic_instructions() -> str:
                    # Always read via the compositor_meta resources over MCP, not Python internals
                    meta = CompositorMetaClient(mcp_client)
                    states = await meta.list_states()  # dict[name -> ServerEntry]
                    text: str = render_compositor_instructions(states)
                    return text

                # Ensure policy reader is initialized by _attach_inproc_servers
                if self._policy_reader is None:
                    raise RuntimeError("policy reader not initialized")
                assert self.approval_hub is not None

                async def _pending_notifier(
                    call_id: str, tool_key: str, args_json: str | None
                ) -> None:
                    if self._cm is not None and self.session is not None:
                        await self._cm.send_payload(
                            ApprovalPendingEvt(
                                call_id=call_id, tool_key=tool_key, args_json=args_json
                            )
                        )

                install_policy_gateway(
                    comp,
                    hub=self.approval_hub,
                    pending_notifier=_pending_notifier,
                    record_outcome=lambda call_id, tool_key, outcome: asyncio.create_task(
                        self.record_policy_outcome(call_id, tool_key, ApprovalOutcome(outcome))
                    ),
                    policy_reader=self._policy_reader,
                )

                # Mount standard in-proc servers (resources, compositor_meta, compositor_admin)
                await mount_standard_inproc_servers(compositor=comp, gateway_client=mcp_client)

                # Start agent
                agent = await MiniCodex.create(
                    model=self.model,
                    mcp_client=mcp_client,
                    system=base_system,
                    client=client,
                    handlers=handlers,
                    dynamic_instructions=_dynamic_instructions,
                )
                await self._stack.enter_async_context(agent)
                # Session tracks the system used for persisted run metadata; store base system
                sess.attach_agent(agent, model=self.model, system=base_system)
                if self.with_ui and self._ui_bus is not None:
                    self.ui = UiFacet(manager=self._cm, ui_bus=self._ui_bus)

                # Publish to container
                self.session = sess
                self.agent = agent
                self.persist_handler = persist_handler
                return None
            case _ReconfigureMsg(mcp_config=mcp_cfg, attach=attach, detach=detach):
                # Inline former _op_reconfigure
                comp2 = self._compositor
                if comp2 is None:
                    return None
                if self._mcp_client is None:
                    raise RuntimeError("mcp client not initialized")
                admin = CompositorAdminClient(self._mcp_client)
                # Compute current specs for diffs
                current_specs = await comp2.mount_specs()
                # Full replace
                if mcp_cfg is not None:
                    desired = mcp_cfg.mcpServers or {}
                    # Detach missing (parallel)
                    miss = list(set(current_specs.keys()) - set(desired.keys()))
                    if miss:
                        await asyncio.gather(*(admin.detach_server(name=n) for n in miss))
                    # Attach new or changed (parallel)
                    attach_args: list[tuple[str, MCPServerTypes]] = []
                    for name, spec in desired.items():
                        prev = current_specs.get(name)
                        if prev is None or prev.model_dump(mode="json") != spec.model_dump(
                            mode="json"
                        ):
                            attach_args.append((name, spec))
                    if attach_args:
                        await asyncio.gather(
                            *(admin.attach_server(name=n, spec=s) for (n, s) in attach_args)
                        )
                # Incremental detach
                if detach:
                    await asyncio.gather(*(admin.detach_server(name=n) for n in detach))
                # Incremental attach
                for _, cfg in (attach or {}).items():
                    latest_specs = await comp2.mount_specs()
                    attach_args2: list[tuple[str, MCPServerTypes]] = []
                    for name, spec in (cfg.mcpServers or {}).items():
                        prev = latest_specs.get(name)
                        if prev is None or prev.model_dump(mode="json") != spec.model_dump(
                            mode="json"
                        ):
                            attach_args2.append((name, spec))
                    if attach_args2:
                        await asyncio.gather(
                            *(admin.attach_server(name=n, spec=s) for (n, s) in attach_args2)
                        )
                await self._push_snapshot_and_status()
                return None
            case _SamplingSnapshotMsg():
                if self._compositor is None:
                    return None
                return await self._compositor.sampling_snapshot()
            case _SamplingSnapshotIncrementalMsg():
                # Emit a single compositor-derived snapshot (compositor is always present)
                if self._compositor is None or self._cm is None or self.session is None:
                    return None
                snap = await self._compositor.sampling_snapshot()
                await self._cm.send_payload(await self.session.build_snapshot(sampling=snap))
                return None
            case _CloseMsg():
                return await self._op_close()
            case _AttachOneMsg(name=name, spec=spec):
                # Route via compositor_admin tools (policy-gated)
                if self._mcp_client is None:
                    raise RuntimeError("mcp client not initialized")
                admin = CompositorAdminClient(self._mcp_client)
                await admin.attach_server(name=name, spec=spec)
                await self._push_snapshot_and_status()
                return None
            case _DetachOneMsg(name=name):
                if self._mcp_client is None:
                    raise RuntimeError("mcp client not initialized")
                admin = CompositorAdminClient(self._mcp_client)
                await admin.detach_server(name=name)
                await self._push_snapshot_and_status()
                return None
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

    async def reconfigure_mcp(
        self,
        *,
        mcp_config: MCPConfig | None = None,
        attach: dict[str, MCPConfig] | None = None,
        detach: list[str] | None = None,
    ) -> None:
        attach_payload = attach if attach is not None else {}
        detach_payload = detach if detach is not None else []
        await self._post_msg(
            _ReconfigureMsg(
                mcp_config=mcp_config,
                attach=attach_payload,
                detach=detach_payload,
            )
        )

    async def attach_mcp(self, name: str, spec: MCPServerTypes) -> None:
        """Attach a single server live via the actor."""
        await self._post_msg(_AttachOneMsg(name=name, spec=spec))

    async def detach_mcp(self, name: str) -> None:
        """Detach a single server live via the actor."""
        await self._post_msg(_DetachOneMsg(name=name))

    async def sampling_snapshot(self) -> SamplingSnapshot | None:
        """Return a structured snapshot of servers/tools via the actor."""
        res = await self._post_msg(_SamplingSnapshotMsg())
        return cast(SamplingSnapshot | None, res)

    async def sampling_snapshot_incremental(self) -> None:
        """Start streaming sampling snapshots as MCP servers initialize."""
        await self._post_msg(_SamplingSnapshotIncrementalMsg())

    async def record_policy_outcome(
        self, call_id: str, tool_key: str, outcome: ApprovalOutcome
    ) -> None:
        if self.session is None:
            return
        run_id = self.session.active_run.run_id if self.session.active_run else None
        if not run_id:
            return
        await self.persistence.record_approval(
            run_id=run_id,
            agent_id=None,
            call_id=call_id,
            tool_key=tool_key,
            outcome=outcome,
            decided_at=datetime.now(UTC),
        )

    async def _attach_inproc_servers(self, ui_bus: ServerBus | None) -> None:
        engine = self.approval_engine
        assert engine is not None

        async def _push_snapshot() -> None:
            if self.session is not None and self._cm is not None:
                await self._cm.send_payload(await self.session.build_snapshot())

        # Mount approval policy servers: reader + proposer (agent container)
        assert self._compositor is not None
        reader_server = ApprovalPolicyServer(
            engine,
            name=APPROVAL_POLICY_SERVER_NAME_READER,
        )
        await self._compositor.mount_inproc(APPROVAL_POLICY_SERVER_NAME_READER, reader_server)
        proposer_server = ApprovalPolicyProposerServer(
            engine=engine,
            name=APPROVAL_POLICY_SERVER_NAME_PROPOSER,
        )
        await self._compositor.mount_inproc(APPROVAL_POLICY_SERVER_NAME_PROPOSER, proposer_server)
        # Admin (approver) server is NOT mounted into the compositor. It is exposed only
        # via a private client held by the container for user/admin HTTP flows.
        approver_server = ApprovalPolicyAdminServer(
            engine=engine,
            name=APPROVAL_POLICY_SERVER_NAME_APPROVER,
        )
        # Create in-proc client to the reader for policy gateway middleware
        _policy_reader_client = Client(reader_server)
        await self._stack.enter_async_context(_policy_reader_client)
        policy_reader = PolicyReaderClient(_policy_reader_client)
        self._policy_reader = policy_reader
        # Create in-proc client for admin operations and keep on container
        _policy_approver_client = Client(approver_server)
        await self._stack.enter_async_context(_policy_approver_client)
        self._policy_approver = PolicyApproverClient(_policy_approver_client)

        if self.with_ui and ui_bus is not None:
            # UI server (in-proc)
            ui_server = make_ui_server("UI", ui_bus)
            await self._compositor.mount_inproc(UI_SERVER_NAME, ui_server)
            # Chat servers (human/assistant) with persisted store bound to agent
            await attach_persisted_chat_servers(
                self._compositor,
                persistence=self.persistence,
                agent_id=self.agent_id,
            )

            # Runtime exec server (no host mounts)
            runtime_image = resolve_runtime_image()
            opts = ContainerOptions(image=runtime_image, volumes=None, ephemeral=True)
            runtime_server = make_runtime_server(opts)
            # Ensure tool is exposed under expected name
            tools = await runtime_server._tool_manager.list_tools()
            assert RUNTIME_EXEC_TOOL_NAME in [t.name for t in tools]
            await self._compositor.mount_inproc(RUNTIME_SERVER_NAME, runtime_server)
        # Persist runtime ephemerality for status reporting (explicit)
        self.runtime_ephemeral = False
        # Notification hooks are managed by the compositor client notifications buffer

    # Seatbelt is no longer intercepted/rewritten; respect provided specs.

    async def _attach_wired_seatbelt(self) -> None:
        if self._compositor is None:
            raise RuntimeError("compositor not initialized")
        await attach_seatbelt_exec(
            self._compositor,
            agent_id=self.agent_id,
            persistence=self.persistence,
            docker_client=self.docker_client,
            name=SEATBELT_EXEC_SERVER_NAME,
        )

    async def _push_snapshot_and_status(self) -> None:
        """Emit a fresh snapshot and broadcast live/working status."""
        if self.session is None or self._cm is None:
            return
        sampling = await self.sampling_snapshot()
        await self._cm.send_payload(await self.session.build_snapshot(sampling=sampling))
        active = self.session.active_run.run_id if self.session.active_run else None
        await self._cm.broadcast_status(True, active)

    # sampling snapshot helpers are inlined in the actor dispatch handlers

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
            error=type(drain_error).__name__
            if (not drained_ok and drain_error is not None)
            else None,
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
    agent_id: str,
    mcp_config: MCPConfig,
    persistence: SQLitePersistence,
    model: str,
    client_factory: Callable[[str], OpenAIModelProto],
    with_ui: bool = True,
    system: str | None = None,
    docker_client: DockerClient,
    initial_policy: str | None = None,
) -> AgentContainer:
    c = AgentContainer(
        agent_id=agent_id,
        persistence=persistence,
        model=model,
        client_factory=client_factory,
        with_ui=with_ui,
        docker_client=docker_client,
        system_override=system,
        initial_policy=initial_policy,
    )
    await c.start(mcp_config=mcp_config)
    return c
