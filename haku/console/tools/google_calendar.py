"""Google Calendar event creation behind haku-console's Google tool provider."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# The Google Calendar REST API speaks camelCase; request-body models below carry `to_camel`
# aliases so `model_dump(by_alias=True, exclude_none=True)` yields the wire shape directly,
# instead of hand-assembling dicts.
_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class EventDateTime(BaseModel):
    """Mirrors the Google Calendar API's `EventDateTime` — exactly one of `date`
    (all-day) or `date_time` (+`time_zone`) is set. Snake-case tool-arg surface (Haku passes
    these); `_EventDateTime` below is the camelCase request-body twin."""

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


# --- Google Calendar `events.insert` request-body models (camelCase wire shape) ---
class _EventDateTime(BaseModel):
    model_config = _CAMEL

    date: str | None = None
    date_time: str | None = None
    time_zone: str | None = None

    @classmethod
    def of(cls, value: EventDateTime) -> _EventDateTime:
        return cls(date=value.date, date_time=value.date_time, time_zone=value.time_zone)


class _ReminderOverride(BaseModel):
    method: str
    minutes: int


class _Reminders(BaseModel):
    model_config = _CAMEL

    use_default: bool
    overrides: list[_ReminderOverride]


class _Attendee(BaseModel):
    email: str


class _EventInsert(BaseModel):
    model_config = _CAMEL

    summary: str
    start: _EventDateTime
    end: _EventDateTime
    description: str | None = None
    location: str | None = None
    attendees: list[_Attendee] | None = None
    reminders: _Reminders | None = None


class CalendarToolsClient:
    def __init__(self, service: Any) -> None:
        self._service = service

    def create_event(self, args: CreateCalendarEventArgs) -> CreateCalendarEventResult:
        body = _EventInsert(
            summary=args.summary,
            start=_EventDateTime.of(args.start),
            end=_EventDateTime.of(args.end),
            description=args.description,
            location=args.location,
            attendees=[_Attendee(email=email) for email in args.attendees] or None,
            reminders=_Reminders(
                use_default=False,
                overrides=[_ReminderOverride(method=r.method, minutes=r.minutes_before_start) for r in args.reminders],
            )
            if args.reminders
            else None,
        )
        created = (
            self._service.events()
            .insert(calendarId=args.calendar_id, body=body.model_dump(by_alias=True, exclude_none=True))
            .execute()
        )
        return CreateCalendarEventResult(event_id=created["id"], html_link=created["htmlLink"])
