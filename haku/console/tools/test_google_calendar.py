"""Tests for the in-process `google_calendar` MCP server (build_mcp)."""

from unittest.mock import Mock, patch

import pytest_bazel
from fastmcp import Client

from haku.console.tools.google_calendar import GOOGLE_CALENDAR_SERVER_ID, build_mcp
from haku.console.tools.google_calendar_client import CalendarEvent, CalendarEventsPage, CalendarSummary


def _mcp(calendar=None):
    return build_mcp(calendar or Mock())


async def test_tool_surface():
    async with Client(_mcp()) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"calendar_summary", "create_event", "get_event", "list_event_instances", "list_events"}
    assert GOOGLE_CALENDAR_SERVER_ID == "google_calendar"


async def test_create_event_dispatches_to_calendar_client():
    calendar = Mock()
    calendar.create_event.return_value = CalendarEvent(event_id="evt1", html_link="https://x/evt1")
    async with Client(_mcp(calendar=calendar)) as client:
        result = await client.call_tool(
            "create_event",
            {
                "summary": "S",
                "start": {"date": "2026-09-15"},
                "end": {"date": "2026-09-16"},
                "recurrence": ["RRULE:FREQ=YEARLY;COUNT=3"],
            },
        )
    assert not result.is_error
    assert result.data.event_id == "evt1"
    (args,), _kwargs = calendar.create_event.call_args
    assert args.summary == "S"
    assert args.recurrence == ["RRULE:FREQ=YEARLY;COUNT=3"]


async def test_create_event_accepts_explicit_null_lists():
    calendar = Mock()
    calendar.create_event.return_value = CalendarEvent(event_id="evt1", html_link="https://x/evt1")
    async with Client(_mcp(calendar=calendar)) as client:
        result = await client.call_tool(
            "create_event",
            {
                "summary": "S",
                "start": {"date": "2026-09-15"},
                "end": {"date": "2026-09-16"},
                "reminders": None,
                "attendees": None,
                "recurrence": None,
            },
        )
    assert not result.is_error
    (args,), _kwargs = calendar.create_event.call_args
    assert args.reminders == []
    assert args.attendees == []
    assert args.recurrence is None


async def test_read_tools_dispatch_to_calendar_client():
    calendar = Mock()
    calendar.get_event.return_value = CalendarEvent(event_id="series1", recurrence=["RRULE:FREQ=WEEKLY"])
    calendar.list_events.return_value = CalendarEventsPage(events=[CalendarEvent(event_id="series1")])
    calendar.list_event_instances.return_value = CalendarEventsPage(
        events=[CalendarEvent(event_id="instance1", recurring_event_id="series1")]
    )
    async with Client(_mcp(calendar=calendar)) as client:
        get_result = await client.call_tool("get_event", {"event_id": "series1"})
        list_result = await client.call_tool("list_events", {"expand_recurring": True, "max_results": 25})
        instances_result = await client.call_tool(
            "list_event_instances", {"recurring_event_id": "series1", "page_token": "page-2"}
        )
    assert get_result.data.event_id == "series1"
    assert list_result.data.events[0].event_id == "series1"
    assert instances_result.data.events[0].recurring_event_id == "series1"
    calendar.get_event.assert_called_once_with("primary", "series1")
    list_args = calendar.list_events.call_args.args[0]
    assert list_args.expand_recurring is True
    assert list_args.max_results == 25
    instance_args = calendar.list_event_instances.call_args.args[0]
    assert instance_args.recurring_event_id == "series1"
    assert instance_args.page_token == "page-2"


async def test_calendar_summary_tool_resolves_over_the_client_service() -> None:
    summary = CalendarSummary(calendar_id="c1", summary="Team (SF)", html_link="https://calendar.example/c1")
    calendar = Mock()
    with patch("haku.console.tools.google_calendar.resolve_calendar_summary", return_value=summary) as resolve_mock:
        async with Client(_mcp(calendar)) as client:
            result = await client.call_tool("calendar_summary", {"calendar_id": "c1"})
    assert not result.is_error
    assert result.structured_content["summary"] == "Team (SF)"
    resolve_mock.assert_called_once_with(calendar.service, "c1")


if __name__ == "__main__":
    pytest_bazel.main()
