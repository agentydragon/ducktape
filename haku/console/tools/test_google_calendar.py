"""Tests for the in-process `google_calendar` MCP server (build_mcp) and the calendar-summary
router endpoint."""

from unittest.mock import Mock, patch

import pytest_bazel
from fastmcp import Client

from haku.console.tools.google_calendar import GOOGLE_CALENDAR_SERVER_ID, build_mcp
from haku.console.tools.google_calendar_client import CalendarSummary, CreateCalendarEventResult


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


async def test_create_calendar_event_accepts_explicit_null_lists():
    calendar = Mock()
    calendar.create_event.return_value = CreateCalendarEventResult(event_id="evt1", html_link="https://x/evt1")
    async with Client(_mcp(calendar=calendar)) as client:
        result = await client.call_tool(
            "create_calendar_event",
            {
                "summary": "S",
                "start": {"date": "2026-09-15"},
                "end": {"date": "2026-09-16"},
                "reminders": None,
                "attendees": None,
            },
        )
    assert not result.is_error
    (args,), _kwargs = calendar.create_event.call_args
    assert args.reminders == []
    assert args.attendees == []


def test_calendar_summary_endpoint_503s_when_unconfigured(make_client) -> None:
    with make_client() as http_client:
        resp = http_client.get("/api/google-calendar/calendar-summary", params={"calendar_id": "c1"})
        assert resp.status_code == 503


def test_calendar_summary_endpoint_resolves_over_the_client_service(make_client) -> None:
    summary = CalendarSummary(calendar_id="c1", summary="Team (SF)", html_link="https://calendar.example/c1")
    calendar = Mock()  # non-None so the endpoint doesn't 503; its .service is handed to the reader
    with (
        patch("haku.console.tools.google_calendar.resolve_calendar_summary", return_value=summary) as resolve_mock,
        make_client(calendar_client=calendar) as http_client,
    ):
        resp = http_client.get("/api/google-calendar/calendar-summary", params={"calendar_id": "c1"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "Team (SF)"
    resolve_mock.assert_called_once_with(calendar.service, "c1")


if __name__ == "__main__":
    pytest_bazel.main()
