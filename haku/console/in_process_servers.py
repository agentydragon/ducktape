"""Canonical construction of haku-console's same-process MCP servers.

The registry holds *builders* (`InProcessServers`): the gmail/google_calendar servers are
built per execution from the acting Operator's Google access token, hostexec from the acting
Operator's Authentik access token (bound by argument, no ambient state), while routine is
credential-free. See `mcp_config.InProcessServerBuilder`.
"""

from __future__ import annotations

from dataclasses import dataclass

import haku.console.tools.gmail as gmail_tools
import haku.console.tools.google_calendar as google_calendar_tools
import haku.console.tools.hostexec as hostexec_tools
import haku.console.tools.routine as routine_tools
from haku.console.config import HostexecConfig
from haku.console.mcp_config import (
    InProcessCredentialKind,
    InProcessServerRegistration,
    InProcessServers,
    const_in_process_server,
)
from haku.console.node_daemons import NodeDaemonService
from haku.console.tools.hostexec_client import HostexecClient
from haku.console.tools.hostexec_token import HostexecJwtBearerExchanger


@dataclass(frozen=True, slots=True)
class HostexecServerConfig:
    """Everything the hostexec builder needs beyond the per-call operator token: the in-scope hosts
    and the Authentik token endpoint (derived once from the operator OIDC issuer at composition)."""

    config: HostexecConfig
    token_endpoint: str


@dataclass(frozen=True, slots=True)
class InProcessServerDependencies:
    """Runtime collaborators for the in-process servers.

    gmail/google_calendar need none (built per call from the acting Operator's token); routine is
    registered only when its launcher is configured; hostexec only when its config is set.
    """

    routine_launcher: routine_tools.RoutineLauncher | None = None
    hostexec: HostexecServerConfig | None = None
    node_daemons: NodeDaemonService | None = None


def build_in_process_servers(dependencies: InProcessServerDependencies) -> InProcessServers:
    """Build the per-call builder for every configured in-process server."""

    servers: InProcessServers = {
        gmail_tools.GMAIL_SERVER_ID: InProcessServerRegistration(
            builder=lambda token: gmail_tools.build_mcp(gmail_tools.build_gmail_client_from_token(token)),
            credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION,
        ),
        google_calendar_tools.GOOGLE_CALENDAR_SERVER_ID: InProcessServerRegistration(
            builder=lambda token: google_calendar_tools.build_mcp(
                google_calendar_tools.build_calendar_client_from_token(token)
            ),
            credential_kind=InProcessCredentialKind.OPERATOR_CONNECTION,
        ),
    }
    if dependencies.routine_launcher is not None:
        servers[routine_tools.HAKU_ROUTINE_SERVER_ID] = const_in_process_server(
            routine_tools.build_mcp(dependencies.routine_launcher)
        )
    if (hostexec := dependencies.hostexec) is not None:
        if dependencies.node_daemons is None:
            raise ValueError("hostexec requires the node-daemon broker")
        daemon_ids = {host: entry.daemon_id for host, entry in hostexec.config.hosts.items()}
        audience_client_ids = {host: entry.audience_client_id for host, entry in hostexec.config.hosts.items()}
        servers[hostexec_tools.HOSTEXEC_SERVER_ID] = InProcessServerRegistration(
            builder=lambda token: hostexec_tools.build_mcp(
                HostexecClient(
                    daemon_ids=daemon_ids,
                    exchange=HostexecJwtBearerExchanger(
                        operator_token=token,
                        token_endpoint=hostexec.token_endpoint,
                        audience_client_ids=audience_client_ids,
                        scope=hostexec.config.exchange_scope,
                    ).exchange,
                    broker=dependencies.node_daemons,
                )
            ),
            credential_kind=InProcessCredentialKind.OPERATOR_LOGIN_IDENTITY,
        )
    return servers
