"""Tests for the in-process `google_calendar` MCP server (build_mcp)."""

from unittest.mock import Mock

import pytest_bazel
from fastmcp import Client

from haku.console.tools.google_calendar import GOOGLE_CALENDAR_SERVER_ID, build_mcp
from haku.console.tools.google_calendar_client import CreateCalendarEventResult


def _mcp(calendar=None):
    return build_mcp(calendar or Mock())


async def test_tool_surface():
    async with Client(_mcp()) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"create_calendar_event"}
    assert GOOGLE_CALENDAR_SERVER_ID == "google_calendar"


async def test_create_calendar_event_dispatches_to_calendar_client():
    calendar = Mock()
    calendar.create_event.return_value = CreateCalendarEventResult(event_id="evt1", html_link="https://x/evt1")
    async with Client(_mcp(calendar=calendar)) as client:
        result = await client.call_tool(
            "create_calendar_event", {"summary": "S", "start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}
        )
    assert not result.is_error
    assert result.data.event_id == "evt1"
    (args,), _kwargs = calendar.create_event.call_args
    assert args.summary == "S"


if __name__ == "__main__":
    pytest_bazel.main()
