"""Google Calendar event creation behind haku-console's Google tool provider."""

from __future__ import annotations

from typing import Any

from haku.console.google_tools_models import CreateCalendarEventArgs, CreateCalendarEventResult, EventDateTime


class CalendarToolsClient:
    def __init__(self, service: Any) -> None:
        self._service = service

    def create_event(self, args: CreateCalendarEventArgs) -> CreateCalendarEventResult:
        body: dict[str, Any] = {
            "summary": args.summary,
            "start": _event_date_time(args.start),
            "end": _event_date_time(args.end),
        }
        if args.description is not None:
            body["description"] = args.description
        if args.location is not None:
            body["location"] = args.location
        if args.attendees:
            body["attendees"] = [{"email": email} for email in args.attendees]
        if args.reminders:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": r.method, "minutes": r.minutes_before_start} for r in args.reminders],
            }
        created = self._service.events().insert(calendarId=args.calendar_id, body=body).execute()
        return CreateCalendarEventResult(event_id=created["id"], html_link=created["htmlLink"])


def _event_date_time(value: EventDateTime) -> dict[str, str]:
    if value.date is not None:
        return {"date": value.date}
    # enforced by EventDateTime's validator
    assert value.date_time is not None
    assert value.time_zone is not None
    return {"dateTime": value.date_time, "timeZone": value.time_zone}
