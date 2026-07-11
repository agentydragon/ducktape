"""Google Calendar event creation behind haku-console's Google tool provider."""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# Opens a specific calendar in the Google Calendar web UI. `cid` is the base64 of the calendar
# id (Google strips the `=` padding), the same encoding its own "add by URL"/share links use.
_CALENDAR_URL = "https://calendar.google.com/calendar/u/0/r?cid={cid}"


class ReminderMethod(StrEnum):
    POPUP = "popup"
    EMAIL = "email"


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
    method: ReminderMethod = ReminderMethod.POPUP
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
    # Parsed straight from the Calendar API's Event response via `model_validate`; the wire
    # fields are `id`/`htmlLink`. populate_by_name keeps field-name construction working too.
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(validation_alias="id")
    html_link: str = Field(validation_alias="htmlLink", description="Link to the event in Google Calendar.")


class _CalendarListEntry(BaseModel):
    """The slice of the Calendar API's `calendarList` entry the approval UI needs."""

    model_config = ConfigDict(populate_by_name=True)

    summary: str
    # The operator's own name for the calendar, when they've renamed it; wins over `summary`.
    summary_override: str | None = Field(default=None, validation_alias="summaryOverride")


class CalendarSummary(BaseModel):
    """A calendar's display name plus a link into the Google Calendar web UI, for rendering a
    pending `create_calendar_event` approval — the tool call carries only the `calendar_id`, so
    the approval UI resolves the human-readable name here rather than showing the raw id."""

    calendar_id: str
    summary: str = Field(description="The calendar's display name (the operator's override if set).")
    html_link: str = Field(description="Link to open this calendar in the Google Calendar web UI.")


# --- Google Calendar `events.insert` request-body models (camelCase wire shape) ---
class _EventDateTime(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    date: str | None = None
    date_time: str | None = None
    time_zone: str | None = None

    @classmethod
    def of(cls, value: EventDateTime) -> _EventDateTime:
        return cls(date=value.date, date_time=value.date_time, time_zone=value.time_zone)


class _ReminderOverride(BaseModel):
    method: ReminderMethod
    minutes: int


class _Reminders(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    use_default: bool
    overrides: list[_ReminderOverride]


class _Attendee(BaseModel):
    email: str


class _EventInsert(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    summary: str
    start: _EventDateTime
    end: _EventDateTime
    description: str | None = None
    location: str | None = None
    attendees: list[_Attendee] | None = None
    reminders: _Reminders | None = None


class CalendarToolsClient:
    """Calendar event creation over a raw Calendar service. Calendar-name resolution is a
    rendering-support read, not a tool op — see the module-level `resolve_calendar_summary`."""

    def __init__(self, service: Any) -> None:
        self.service = service

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
            self.service.events()
            .insert(calendarId=args.calendar_id, body=body.model_dump(by_alias=True, exclude_none=True))
            .execute()
        )
        return CreateCalendarEventResult.model_validate(created)


def resolve_calendar_summary(service: Any, calendar_id: str) -> CalendarSummary:
    """The calendar's display name + a Google Calendar link, for rendering a pending
    `create_calendar_event` approval. `calendarList` carries the operator's own naming
    (`summaryOverride`), which the mounted token's `calendar.readonly` scope covers — a
    rendering read, not a tool the agent invokes, so a free function over the raw service."""
    entry = _CalendarListEntry.model_validate(service.calendarList().get(calendarId=calendar_id).execute())
    cid = base64.b64encode(calendar_id.encode()).decode().rstrip("=")
    return CalendarSummary(
        calendar_id=calendar_id,
        summary=entry.summary_override or entry.summary,
        html_link=_CALENDAR_URL.format(cid=cid),
    )
