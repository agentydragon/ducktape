"""Canonical construction of haku-console's same-process MCP servers."""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp import FastMCP

import haku.console.tools.gmail as gmail_tools
import haku.console.tools.google_calendar as google_calendar_tools
import haku.console.tools.routine as routine_tools
from haku.console.tools.gmail_client import GmailToolsClient
from haku.console.tools.google_calendar_client import CalendarToolsClient


@dataclass(frozen=True, slots=True)
class InProcessServerDependencies:
    """Available runtime collaborators; ``None`` leaves that server disabled."""

    gmail: GmailToolsClient | None = None
    calendar: CalendarToolsClient | None = None
    routine_launcher: routine_tools.RoutineLauncher | None = None


def build_in_process_servers(dependencies: InProcessServerDependencies) -> dict[str, FastMCP]:
    """Build every configured in-process server from the single canonical catalog."""

    servers: dict[str, FastMCP] = {}
    if dependencies.gmail is not None:
        servers[gmail_tools.GMAIL_SERVER_ID] = gmail_tools.build_mcp(dependencies.gmail)
    if dependencies.calendar is not None:
        servers[google_calendar_tools.GOOGLE_CALENDAR_SERVER_ID] = google_calendar_tools.build_mcp(
            dependencies.calendar
        )
    if dependencies.routine_launcher is not None:
        servers[routine_tools.HAKU_ROUTINE_SERVER_ID] = routine_tools.build_mcp(dependencies.routine_launcher)
    return servers
