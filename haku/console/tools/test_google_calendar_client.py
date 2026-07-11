"""Tests for CalendarToolsClient and resolve_calendar_summary over a fake googleapiclient-shaped
Calendar service."""

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_bazel

from haku.console.tools.google_calendar_client import (
    CalendarReminder,
    CalendarToolsClient,
    CreateCalendarEventArgs,
    EventDateTime,
    resolve_calendar_summary,
)


@dataclass
class _FakeEvents:
    inserted: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def insert(self, *, calendarId, body):  # noqa: N803 -- mirrors Calendar API's kwarg casing
        self.inserted.append((calendarId, body))
        return _FakeExecutable({"id": "evt1", "htmlLink": "https://calendar.google.com/event?eid=evt1"})


class _FakeExecutable:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self) -> dict[str, Any]:
        return self._result


@dataclass
class _FakeCalendarList:
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, *, calendarId):  # noqa: N803 -- mirrors Calendar API's kwarg casing
        return _FakeExecutable(self.entries[calendarId])


@dataclass
class _FakeCalendarService:
    events_: _FakeEvents = field(default_factory=_FakeEvents)
    calendar_list_: _FakeCalendarList = field(default_factory=_FakeCalendarList)

    def events(self) -> _FakeEvents:
        return self.events_

    def calendarList(self) -> _FakeCalendarList:  # noqa: N802 -- mirrors Calendar API's camelCase
        return self.calendar_list_


@pytest.fixture
def service() -> _FakeCalendarService:
    return _FakeCalendarService()


@pytest.fixture
def client(service: _FakeCalendarService) -> CalendarToolsClient:
    return CalendarToolsClient(service)


def test_create_event_timed_with_reminders_and_attendees(
    service: _FakeCalendarService, client: CalendarToolsClient
) -> None:
    args = CreateCalendarEventArgs(
        summary="Pay CA estimated tax",
        start=EventDateTime(date_time="2026-09-15T09:00:00-07:00", time_zone="America/Los_Angeles"),
        end=EventDateTime(date_time="2026-09-15T09:30:00-07:00", time_zone="America/Los_Angeles"),
        reminders=[
            CalendarReminder(method="popup", minutes_before_start=60),
            CalendarReminder(method="email", minutes_before_start=1440),
        ],
        attendees=["michael@example.com"],
    )
    result = client.create_event(args)
    assert result.event_id == "evt1"
    assert result.html_link == "https://calendar.google.com/event?eid=evt1"
    calendar_id, body = service.events_.inserted[0]
    assert calendar_id == "primary"
    assert body["start"] == {"dateTime": "2026-09-15T09:00:00-07:00", "timeZone": "America/Los_Angeles"}
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 60}, {"method": "email", "minutes": 1440}],
    }
    assert body["attendees"] == [{"email": "michael@example.com"}]


def test_create_event_all_day_omits_reminders_when_unset(
    service: _FakeCalendarService, client: CalendarToolsClient
) -> None:
    args = CreateCalendarEventArgs(
        summary="Federal estimated tax due",
        start=EventDateTime(date="2026-09-15"),
        end=EventDateTime(date="2026-09-16"),
    )
    client.create_event(args)
    _calendar_id, body = service.events_.inserted[0]
    assert body["start"] == {"date": "2026-09-15"}
    assert "reminders" not in body


def test_event_date_time_requires_exactly_one_of_date_or_date_time() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        EventDateTime()
    with pytest.raises(ValueError, match="exactly one of"):
        EventDateTime(date="2026-09-15", date_time="2026-09-15T09:00:00-07:00", time_zone="UTC")


def test_resolve_calendar_summary_prefers_override_and_links_by_cid(service: _FakeCalendarService) -> None:
    service.calendar_list_.entries["team@group.calendar.google.com"] = {
        "summary": "Team",
        "summaryOverride": "Team (SF)",  # the operator's own rename wins over the calendar's own name
    }
    result = resolve_calendar_summary(service, "team@group.calendar.google.com")
    assert result.calendar_id == "team@group.calendar.google.com"
    assert result.summary == "Team (SF)"
    cid = base64.b64encode(b"team@group.calendar.google.com").decode().rstrip("=")
    assert result.html_link == f"https://calendar.google.com/calendar/u/0/r?cid={cid}"


def test_resolve_calendar_summary_uses_summary_without_override(service: _FakeCalendarService) -> None:
    service.calendar_list_.entries["c1"] = {"summary": "Holidays"}
    assert resolve_calendar_summary(service, "c1").summary == "Holidays"


if __name__ == "__main__":
    pytest_bazel.main()
