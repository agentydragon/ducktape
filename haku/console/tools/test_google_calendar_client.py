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
    ListCalendarEventInstancesArgs,
    ListCalendarEventsArgs,
    resolve_calendar_summary,
)


@dataclass
class _FakeEvents:
    inserted: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    gotten: list[tuple[str, str]] = field(default_factory=list)
    listed: list[dict[str, Any]] = field(default_factory=list)
    instances_listed: list[dict[str, Any]] = field(default_factory=list)

    def insert(self, *, calendarId, body):  # noqa: N803 -- mirrors Calendar API's kwarg casing
        self.inserted.append((calendarId, body))
        return _FakeExecutable(
            {
                "id": "evt1",
                "summary": body["summary"],
                "start": body["start"],
                "end": body["end"],
                "recurrence": body.get("recurrence", []),
                "htmlLink": "https://calendar.google.com/event?eid=evt1",
            }
        )

    def get(self, *, calendarId, eventId):  # noqa: N803 -- mirrors Calendar API's kwarg casing
        self.gotten.append((calendarId, eventId))
        return _FakeExecutable({"id": eventId, "summary": "Standup", "recurrence": ["RRULE:FREQ=WEEKLY"]})

    def list(self, **params):
        self.listed.append(params)
        return _FakeExecutable(
            {
                "items": [{"id": "series1", "summary": "Standup", "recurrence": ["RRULE:FREQ=WEEKLY"]}],
                "nextPageToken": "next-events",
            }
        )

    def instances(self, **params):
        self.instances_listed.append(params)
        return _FakeExecutable(
            {
                "items": [
                    {
                        "id": "instance1",
                        "summary": "Standup",
                        "recurringEventId": params["eventId"],
                        "originalStartTime": {
                            "dateTime": "2026-09-15T09:00:00-07:00",
                            "timeZone": "America/Los_Angeles",
                        },
                    }
                ]
            }
        )


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


def test_create_event_passes_validated_rrules_through_unchanged(
    service: _FakeCalendarService, client: CalendarToolsClient
) -> None:
    recurrence = ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12", "RRULE:FREQ=MONTHLY;BYDAY=2TU;COUNT=3"]
    args = CreateCalendarEventArgs(
        summary="Training",
        start=EventDateTime(date_time="2026-09-15T09:00:00-07:00", time_zone="America/Los_Angeles"),
        end=EventDateTime(date_time="2026-09-15T10:00:00-07:00", time_zone="America/Los_Angeles"),
        recurrence=recurrence,
    )
    result = client.create_event(args)
    assert service.events_.inserted[0][1]["recurrence"] == recurrence
    assert result.recurrence == recurrence


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
    assert "recurrence" not in body


def test_event_date_time_requires_exactly_one_of_date_or_date_time() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        EventDateTime()
    with pytest.raises(ValueError, match="exactly one of"):
        EventDateTime(date="2026-09-15", date_time="2026-09-15T09:00:00-07:00", time_zone="UTC")


@pytest.mark.parametrize(
    ("recurrence", "match"),
    [
        ([], "at least 1"),
        ([""], "non-empty unfolded"),
        ([" RRULE:FREQ=DAILY"], "non-empty unfolded"),
        (["RRULE:FREQ=DAILY\nRRULE:FREQ=WEEKLY"], "non-empty unfolded"),
        (["RDATE:20260915"], "only RRULE"),
        (["RRULE:FREQ=NOPE"], "invalid RRULE"),
        (["RRULE:FREQ=DAILY;COUNT=3;UNTIL=20260930"], "both COUNT and UNTIL"),
    ],
)
def test_recurrence_rejects_invalid_input(recurrence: list[str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CreateCalendarEventArgs(
            summary="Bad recurrence",
            start=EventDateTime(date="2026-09-15"),
            end=EventDateTime(date="2026-09-16"),
            recurrence=recurrence,
        )


def test_get_event_returns_focused_series(service: _FakeCalendarService, client: CalendarToolsClient) -> None:
    event = client.get_event("primary", "series1")
    assert event.event_id == "series1"
    assert event.recurrence == ["RRULE:FREQ=WEEKLY"]
    assert service.events_.gotten == [("primary", "series1")]


def test_list_events_maps_filters_and_pagination(service: _FakeCalendarService, client: CalendarToolsClient) -> None:
    page = client.list_events(
        ListCalendarEventsArgs(
            calendar_id="team",
            time_min="2026-09-01T00:00:00Z",
            time_max="2026-10-01T00:00:00Z",
            query="standup",
            expand_recurring=True,
            max_results=75,
            page_token="page-2",
        )
    )
    assert page.events[0].event_id == "series1"
    assert page.next_page_token == "next-events"
    assert service.events_.listed == [
        {
            "calendarId": "team",
            "singleEvents": True,
            "maxResults": 75,
            "timeMin": "2026-09-01T00:00:00Z",
            "timeMax": "2026-10-01T00:00:00Z",
            "q": "standup",
            "pageToken": "page-2",
        }
    ]


def test_list_event_instances_preserves_instance_linkage(
    service: _FakeCalendarService, client: CalendarToolsClient
) -> None:
    page = client.list_event_instances(ListCalendarEventInstancesArgs(recurring_event_id="series1", max_results=25))
    assert page.events[0].recurring_event_id == "series1"
    assert page.events[0].original_start_time is not None
    assert service.events_.instances_listed == [{"calendarId": "primary", "eventId": "series1", "maxResults": 25}]


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
