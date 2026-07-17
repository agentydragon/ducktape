"""Canonical construction of haku-console's same-process MCP servers.

The registry holds *builders* (`InProcessServers`): the gmail/google_calendar servers are
built per execution from the acting Operator's Google access token (bound by argument, no
ambient state), while routine is credential-free. See `mcp_config.InProcessServerBuilder`.
"""

from __future__ import annotations

from dataclasses import dataclass

import haku.console.tools.gmail as gmail_tools
import haku.console.tools.google_calendar as google_calendar_tools
import haku.console.tools.routine as routine_tools
from haku.console.mcp_config import InProcessServers, const_in_process_server


@dataclass(frozen=True, slots=True)
class InProcessServerDependencies:
    """Runtime collaborators for the credential-free in-process servers.

    gmail/google_calendar need none (they are built per call from the acting Operator's token);
    routine is registered only when its launcher is configured.
    """

    routine_launcher: routine_tools.RoutineLauncher | None = None


def build_in_process_servers(dependencies: InProcessServerDependencies) -> InProcessServers:
    """Build the per-call builder for every configured in-process server."""

    servers: InProcessServers = {
        gmail_tools.GMAIL_SERVER_ID: lambda token: gmail_tools.build_mcp(
            gmail_tools.build_gmail_client_from_token(token)
        ),
        google_calendar_tools.GOOGLE_CALENDAR_SERVER_ID: lambda token: google_calendar_tools.build_mcp(
            google_calendar_tools.build_calendar_client_from_token(token)
        ),
    }
    if dependencies.routine_launcher is not None:
        servers[routine_tools.HAKU_ROUTINE_SERVER_ID] = const_in_process_server(
            routine_tools.build_mcp(dependencies.routine_launcher)
        )
    return servers
