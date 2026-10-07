"""Canonical construction of haku-console's same-process MCP servers.

The grants backend remains available to execute approved rows already in the ledger while the
retired MCP submission surface drains. Trusted caller context travels in MCP request metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

import haku.console.tools.grants as grants_tools
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import (
    AccessProfile,
    InProcessCredentialKind,
    InProcessServerRegistration,
    InProcessServers,
)


@dataclass(frozen=True, slots=True)
class InProcessServerDependencies:
    """Runtime collaborators for the in-process servers.

    access profiles govern the grants server while previously queued calls drain.
    """

    access_profiles: tuple[AccessProfile, ...] = ()
    # The unified grant server fronting every grant domain (kubernetes | http) plus the kubernetes
    # SAR check (`kubernetes_can_i`) — one server, no separate `kubernetes` server (#4918).
    grants: grants_tools.GrantsToolsService | None = None


def build_in_process_servers(dependencies: InProcessServerDependencies) -> InProcessServers:
    """Build the per-call builder for every configured in-process server."""

    in_process_access = InProcessServerAccessPolicy(dependencies.access_profiles)
    servers: InProcessServers = {}
    if (grants := dependencies.grants) is not None:
        servers[grants_tools.GRANTS_SERVER_ID] = InProcessServerRegistration(
            builder=lambda _token: grants_tools.build_mcp(grants),
            credential_kind=InProcessCredentialKind.NONE,
            authorizer=in_process_access.authorizer_for(grants_tools.GRANTS_SERVER_ID),
        )
    return servers
