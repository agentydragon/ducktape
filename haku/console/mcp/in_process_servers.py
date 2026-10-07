"""Canonical construction of haku-console's same-process MCP servers.

The registry holds *builders* (`InProcessServers`): routine and grants are credential-free, built
lazily from deploy-time collaborators. Trusted caller context for profile-scoped servers travels
in MCP request metadata. See `execution.McpExecutionContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

import haku.console.tools.grants as grants_tools
import haku.console.tools.routine as routine_tools
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import (
    AccessProfile,
    InProcessCredentialKind,
    InProcessServerRegistration,
    InProcessServers,
    const_in_process_server,
)


@dataclass(frozen=True, slots=True)
class InProcessServerDependencies:
    """Runtime collaborators for the in-process servers.

    routine is registered only when its launcher is configured.
    """

    routine_launcher: routine_tools.RoutineLauncher | None = None
    access_profiles: tuple[AccessProfile, ...] = ()
    # The unified grant server fronting every grant domain (kubernetes | http) plus the kubernetes
    # SAR check (`kubernetes_can_i`) — one server, no separate `kubernetes` server (#4918).
    grants: grants_tools.GrantsToolsService | None = None


def build_in_process_servers(dependencies: InProcessServerDependencies) -> InProcessServers:
    """Build the per-call builder for every configured in-process server."""

    in_process_access = InProcessServerAccessPolicy(dependencies.access_profiles)
    servers: InProcessServers = {}
    if dependencies.routine_launcher is not None:
        servers[routine_tools.HAKU_ROUTINE_SERVER_ID] = const_in_process_server(
            routine_tools.build_mcp(dependencies.routine_launcher)
        )
    if (grants := dependencies.grants) is not None:
        servers[grants_tools.GRANTS_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: grants_tools.build_mcp(grants),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=in_process_access.authorizer_for(grants_tools.GRANTS_SERVER_ID),
        )
    return servers
