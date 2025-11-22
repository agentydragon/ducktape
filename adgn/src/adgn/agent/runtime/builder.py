"""Builder functions for creating agent infrastructure.

Provides build_local_agent() which creates:
- MCPInfrastructure: Core MCP server setup + policy gateway
- LocalAgentRuntime: MiniCodex agent wrapper
"""

from __future__ import annotations

from collections.abc import Callable

from docker import DockerClient
from fastmcp.mcp_config import MCPConfig

from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.runtime.infrastructure import MCPInfrastructure
from adgn.agent.runtime.local_runtime import LocalAgentRuntime
from adgn.agent.runtime.running import RunningInfrastructure
from adgn.agent.runtime.sidecars import ChatSidecar, LoopControlSidecar, UISidecar
from adgn.agent.server.bus import ServerBus
from adgn.agent.server.runtime import ConnectionManager
from adgn.agent.types import AgentID
from adgn.openai_utils.model import OpenAIModelProto


async def build_local_agent(
    *,
    agent_id: AgentID,
    mcp_config: MCPConfig,
    persistence: SQLitePersistence,
    model: str,
    client_factory: Callable[[str], OpenAIModelProto],
    docker_client: DockerClient,
    with_ui: bool = True,
    system_override: str | None = None,
    initial_policy: str | None = None,
) -> tuple[RunningInfrastructure, LocalAgentRuntime, ServerBus | None, ConnectionManager | None]:
    """Build local agent infrastructure and runtime.

    Creates all necessary components for a local agent:
    - MCPInfrastructure with policy gateway
    - ServerBus and ConnectionManager (when with_ui=True)
    - LocalAgentRuntime with UI integration

    Example:
    from adgn.openai_utils.client_factory import build_client

    def client_factory(model: str):
        return build_client(model, enable_debug_logging=True)

    running, runtime, ui_bus, conn_mgr = await build_local_agent(
        agent_id="my-agent",
        mcp_config=config,
        persistence=persistence,
        model="o4-mini",
        client_factory=client_factory,
        docker_client=docker.from_env(),
        with_ui=True,
    )

    # Use the agent
    result = await runtime.run("Hello!")

    # Cleanup
    await runtime.close()
    await running.close()
    """
    builder = MCPInfrastructure(
        agent_id=agent_id,
        persistence=persistence,
        docker_client=docker_client,
        initial_policy=initial_policy,
        connection_manager=ConnectionManager() if with_ui else None,
    )

    running = await builder.start(mcp_config)

    ui_bus: ServerBus | None = None
    if with_ui:
        ui_bus = ServerBus()
        await running.attach_sidecar(UISidecar(ui_bus))
    await running.attach_sidecar(ChatSidecar())
    await running.attach_sidecar(LoopControlSidecar())

    runtime = LocalAgentRuntime(
        running=running,
        model=model,
        client_factory=client_factory,
        system_override=system_override,
        ui_bus=ui_bus,
        connection_manager=builder.connection_manager,
    )

    await runtime.start()

    return (running, runtime, ui_bus, builder.connection_manager)
