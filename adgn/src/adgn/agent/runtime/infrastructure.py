"""MCPInfrastructure - builder for core MCP infrastructure.

This module provides MCPInfrastructure, a factory/builder that creates
RunningInfrastructure instances. The infrastructure includes:

- Compositor (MCP server aggregator)
- Policy Gateway (approval enforcement middleware)
- Approval Policy Engine (Docker-based policy evaluation)
- Standard meta servers (resources, compositor_meta, compositor_admin)

Sidecars (runtime, UI, chat, loop) are attached separately to RunningInfrastructure.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
import logging
import os

from docker import DockerClient
from fastmcp.client import Client
from fastmcp.mcp_config import MCPConfig

from adgn.agent.approvals import ApprovalHub, ApprovalPolicyEngine, load_default_policy_source, make_policy_engine
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.presets import discover_presets
from adgn.agent.runtime.running import RunningInfrastructure
from adgn.agent.server.protocol import ApprovalBrief, ApprovalPendingEvt
from adgn.agent.server.runtime import ConnectionManager
from adgn.agent.types import AgentID, ToolCall
from adgn.mcp._shared.constants import (
    APPROVAL_POLICY_SERVER_NAME_APPROVER,
    APPROVAL_POLICY_SERVER_NAME_PROPOSER,
    APPROVAL_POLICY_SERVER_NAME_READER,
)
from adgn.mcp.approval_policy.clients import PolicyApproverStub, PolicyReaderStub
from adgn.mcp.approval_policy.server import (
    ApprovalPolicyAdminServer,
    ApprovalPolicyProposerServer,
    ApprovalPolicyServer,
)
from adgn.mcp.compositor.server import Compositor
from adgn.mcp.compositor.setup import mount_standard_inproc_servers
from adgn.mcp.notifications.buffer import NotificationsBuffer
from adgn.mcp.policy_gateway.middleware import install_policy_gateway
from adgn.mcp.stubs.typed_stubs import TypedClient

logger = logging.getLogger(__name__)


class MCPInfrastructure:
    """Creates minimal core infrastructure - does NOT include optional sidecars
    (UI, chat, loop, runtime). Those are attached to RunningInfrastructure.

    Example:
        # Create builder
        builder = MCPInfrastructure(
            agent_id="my-agent",
            persistence=persistence,
            docker_client=docker_client,
        )

        # Start core infrastructure
        running = await builder.start(mcp_config)

        # Attach sidecars (UI, chat, loop control)
        from adgn.agent.runtime.sidecars import ChatSidecar, LoopControlSidecar, UISidecar
        await running.attach_sidecar(UISidecar(ui_bus))
        await running.attach_sidecar(ChatSidecar())
        await running.attach_sidecar(LoopControlSidecar())

        # Use it
        tools = await running.compositor_client.list_tools()

        # Cleanup
        await running.close()
    """

    def __init__(
        self,
        agent_id: AgentID,
        persistence: SQLitePersistence,
        docker_client: DockerClient,
        initial_policy: str | None = None,
        connection_manager: ConnectionManager | None = None,
    ):
        self.agent_id = agent_id
        self.persistence = persistence
        self.docker_client = docker_client
        self.initial_policy = initial_policy
        self._connection_manager = connection_manager

    async def start(self, mcp_config: MCPConfig) -> RunningInfrastructure:
        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            approval_engine, approval_hub = await self._setup_approval_infrastructure()

            compositor = Compositor("compositor", eager_open=True)
            for name, server_cfg in mcp_config.mcpServers.items():
                await compositor.mount_server(name, server_cfg)

            notif_buffer = NotificationsBuffer(compositor=compositor)
            compositor_client = Client(compositor, message_handler=notif_buffer.handler)
            await stack.enter_async_context(compositor_client)

            policy_reader, policy_approver = await self._mount_approval_policy_servers(
                compositor, approval_engine, stack
            )

            await self._install_policy_gateway(compositor, approval_hub, policy_reader)

            await mount_standard_inproc_servers(compositor=compositor, gateway_client=compositor_client)

            return RunningInfrastructure(
                compositor=compositor,
                compositor_client=compositor_client,
                notifications_buffer=notif_buffer,
                policy_reader=policy_reader,
                policy_approver=policy_approver,
                approval_engine=approval_engine,
                approval_hub=approval_hub,
                agent_id=self.agent_id,
                _stack=stack,
            )

        except Exception:
            # Cleanup on failure
            await stack.aclose()
            raise

    async def _setup_approval_infrastructure(self) -> tuple[ApprovalPolicyEngine, ApprovalHub]:
        """Resolves the initial policy source (from preset, initial_policy parameter,
        or default) and constructs the approval policy engine.
        """
        row = await self.persistence.get_agent(self.agent_id)
        preset_name: str | None = None
        if row:
            preset_name = row.preset

        presets = discover_presets(os.getenv("ADGN_AGENT_PRESETS_DIR")) if preset_name else {}
        preset = presets.get(preset_name) if preset_name else None

        chosen = (
            self.initial_policy
            or (preset.approval_policy if (preset and preset.approval_policy) else None)
            or load_default_policy_source()
        )

        approval_engine = make_policy_engine(
            agent_id=self.agent_id, persistence=self.persistence, docker_client=self.docker_client, policy_source=chosen
        )

        approval_hub = ApprovalHub()

        return (approval_engine, approval_hub)

    async def _mount_approval_policy_servers(
        self, compositor: Compositor, approval_engine: ApprovalPolicyEngine, stack: AsyncExitStack
    ) -> tuple[PolicyReaderStub, PolicyApproverStub]:
        """Mounts:
            - approval_policy_reader (resources + decide tool)
            - approval_policy_proposer (create/withdraw proposal tools)

        Creates internal clients (not mounted):
            - policy_reader: for policy gateway middleware
            - policy_approver: for HTTP admin API
        """
        reader_server = ApprovalPolicyServer(approval_engine, name=APPROVAL_POLICY_SERVER_NAME_READER)
        await compositor.mount_inproc(APPROVAL_POLICY_SERVER_NAME_READER, reader_server)

        proposer_server = ApprovalPolicyProposerServer(
            engine=approval_engine, name=APPROVAL_POLICY_SERVER_NAME_PROPOSER
        )
        await compositor.mount_inproc(APPROVAL_POLICY_SERVER_NAME_PROPOSER, proposer_server)

        approver_server = ApprovalPolicyAdminServer(engine=approval_engine, name=APPROVAL_POLICY_SERVER_NAME_APPROVER)

        policy_reader = await PolicyReaderStub.for_server(stack, reader_server)
        policy_approver = await PolicyApproverStub.for_server(stack, approver_server)

        return (policy_reader, policy_approver)

    async def _install_policy_gateway(
        self, compositor: Compositor, approval_hub: ApprovalHub, policy_reader: PolicyReaderStub
    ) -> None:
        """The policy gateway intercepts all tool calls and evaluates them
        against the active approval policy before execution.
        """

        async def _pending_notifier(tool_call: ToolCall) -> None:
            """Notify UI of pending approval requests."""
            if self._connection_manager is not None:
                await self._connection_manager.send_payload(
                    ApprovalPendingEvt(approval=ApprovalBrief(tool_call=tool_call))
                )

        install_policy_gateway(
            compositor,
            hub=approval_hub,
            pending_notifier=_pending_notifier,
            policy_reader=policy_reader,
            persistence=self.persistence,
            run_id=None,
            agent_id=self.agent_id,
        )
