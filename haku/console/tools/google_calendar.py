"""Google Calendar event creation behind haku-console's Google tool provider."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class EventDateTime(BaseModel):
    """Mirrors the Google Calendar API's `EventDateTime` — exactly one of `date`
    (all-day) or `date_time` (+`time_zone`) is set."""

    date: str | None = Field(default=None, description="All-day event date, YYYY-MM-DD.")
    date_time: str | None = Field(
        default=None, description="Timed event instant, RFC3339 (e.g. 2026-09-15T09:00:00-07:00)."
    )
    time_zone: str | None = Field(
        default=None, description="IANA time zone (e.g. America/Los_Angeles). Required when date_time is set."
    )

    @model_validator(mode="after")
    def _exactly_one_of_date_or_date_time(self) -> EventDateTime:
        if (self.date is None) == (self.date_time is None):
            raise ValueError("exactly one of date or date_time must be set")
        if self.date_time is not None and self.time_zone is None:
            raise ValueError("time_zone is required when date_time is set")
        return self


class CalendarReminder(BaseModel):
    method: Literal["popup", "email"] = "popup"
    minutes_before_start: int = Field(
        ge=0, le=40320, description="Minutes before the event start; Google's max is 4 weeks."
    )


class CreateCalendarEventArgs(BaseModel):
    """Create a Google Calendar event, optionally with custom reminders and attendees."""

    summary: str = Field(description="Event title.")
    start: EventDateTime
    end: EventDateTime
    description: str | None = Field(default=None, description="Event body text.")
    location: str | None = None
    calendar_id: str = Field(
        default="primary", description="Target calendar; 'primary' is the operator's main calendar."
    )
    reminders: list[CalendarReminder] = Field(
        default_factory=list,
        description="Overrides the calendar's default reminders. Empty means use the calendar default.",
    )
    attendees: list[str] = Field(default_factory=list, description="Attendee email addresses to invite.")


class CreateCalendarEventResult(BaseModel):
    event_id: str
    html_link: str = Field(description="Link to the event in Google Calendar.")


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
