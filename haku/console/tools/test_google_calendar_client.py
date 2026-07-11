"""Tests for CalendarToolsClient over a fake googleapiclient-shaped Calendar service."""

from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_bazel

from haku.console.tools.google_calendar_client import (
    CalendarReminder,
    CalendarToolsClient,
    CreateCalendarEventArgs,
    EventDateTime,
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
class _FakeCalendarService:
    events_: _FakeEvents = field(default_factory=_FakeEvents)

    def events(self) -> _FakeEvents:
        return self.events_


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


if __name__ == "__main__":
    pytest_bazel.main()
