"""Compositor factory functions for agents bridge using two-level compositor pattern."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp.client import Client

from adgn.agent.mcp_bridge.types import AgentID, AgentMode
from adgn.mcp.compositor.server import Compositor
from adgn.mcp.compositor.setup import mount_standard_inproc_servers

if TYPE_CHECKING:
    from adgn.agent.mcp_bridge.server import InfrastructureRegistry

logger = logging.getLogger(__name__)


async def create_agent_compositor(agent_id: AgentID, registry: InfrastructureRegistry) -> Compositor:
    """Create per-agent compositor with infrastructure servers.

    This compositor contains the agent's infrastructure objects (ApprovalPolicyEngine,
    ApprovalHub, etc.) which are now MCP servers themselves. When mounted in the global
    compositor with prefix f"agent{agent_id}", the resources become:

    - resource://agent{id}/policy/policy.py
    - resource://agent{id}/policy/proposals/list
    - resource://agent{id}/approvals/approvals

    Args:
        agent_id: The agent's unique identifier
        registry: Infrastructure registry containing agent infrastructure

    Returns:
        Compositor with mounted infrastructure servers for this agent
    """
    comp = Compositor(f"agent_{agent_id}_infra")

    # Get agent infrastructure
    infra = await registry.get_infrastructure(agent_id)

    await comp.mount_inproc("policy", infra.approval_engine)
    logger.info(f"Mounted approval policy engine for agent {agent_id}")

    await comp.mount_inproc("approvals", infra.approval_hub)
    logger.info(f"Mounted approvals hub for agent {agent_id}")

    # TODO: Mount session server (if local agent with session)
    # if infra.mode == AgentMode.LOCAL and infra.local_runtime:
    #     session_server = SessionStateBridgeServer(infra.local_runtime.session, agent_id)
    #     await comp.mount_inproc("session", session_server)
    #     logger.info(f"Mounted session server for agent {agent_id}")

    return comp


async def create_global_compositor(
    registry: InfrastructureRegistry,
    gateway_client: Client | None = None
) -> Compositor:
    """Create global compositor with all agent compositors and registry.

    This is the top-level compositor that aggregates:
    - Agent registry (resource://registry/agents/list)
    - Per-agent compositors (resource://agent{id}/...)
    - Standard infrastructure (resources, compositor_meta, compositor_admin)

    Args:
        registry: Infrastructure registry managing all agents
        gateway_client: Optional gateway client for resources server

    Returns:
        Global compositor exposing all agents and infrastructure
    """
    global_comp = Compositor("agents_bridge")

    # Set global compositor reference in registry for dynamic mounting
    registry._global_compositor = global_comp

    # Mount registry (now an MCP server itself)
    await global_comp.mount_inproc("registry", registry)
    logger.info("Mounted registry server")

    # Mount per-agent compositors for existing agents
    for agent_id in registry.known_agents():
        agent_comp = await create_agent_compositor(agent_id, registry)
        await global_comp.mount_inproc(f"agent{agent_id}", agent_comp)
        logger.info(f"Mounted agent compositor for agent {agent_id}")

    # Standard infrastructure (resources aggregator, compositor metadata, admin)
    if gateway_client is not None:
        await mount_standard_inproc_servers(global_comp, gateway_client)
        logger.info("Mounted standard infrastructure servers")

    return global_comp


async def mount_agent_compositor_dynamically(
    global_compositor: Compositor,
    agent_id: AgentID,
    registry: InfrastructureRegistry
) -> None:
    """Dynamically mount a new agent's compositor in the global compositor.

    This is called when a new agent is created to add its resources to the
    global compositor without restarting.

    Args:
        global_compositor: The global agents bridge compositor
        agent_id: ID of the newly created agent
        registry: Infrastructure registry
    """
    agent_comp = await create_agent_compositor(agent_id, registry)
    await global_compositor.mount_inproc(f"agent{agent_id}", agent_comp)
    logger.info(f"Dynamically mounted compositor for new agent {agent_id}")


async def unmount_agent_compositor_dynamically(
    global_compositor: Compositor,
    agent_id: AgentID
) -> None:
    """Dynamically unmount an agent's compositor from the global compositor.

    This is called when an agent is deleted to remove its resources from the
    global compositor without restarting.

    Args:
        global_compositor: The global agents bridge compositor
        agent_id: ID of the agent being deleted
    """
    await global_compositor.unmount_server(f"agent{agent_id}")
    logger.info(f"Dynamically unmounted compositor for deleted agent {agent_id}")
