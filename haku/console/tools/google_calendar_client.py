"""Focused Google Calendar event reads and creation for haku-console."""

from __future__ import annotations

import base64
import datetime as dt
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
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
        default=None,
        validation_alias=AliasChoices("date_time", "dateTime"),
        description="Timed event instant, RFC3339 (e.g. 2026-09-15T09:00:00-07:00).",
    )
    time_zone: str | None = Field(
        default=None,
        validation_alias=AliasChoices("time_zone", "timeZone"),
        description="IANA time zone (e.g. America/Los_Angeles). Required when date_time is set.",
    )

    @model_validator(mode="after")
    def _exactly_one_of_date_or_date_time(self) -> EventDateTime:
        if (self.date is None) == (self.date_time is None):
            raise ValueError("exactly one of date or date_time must be set")
        if self.date_time is not None and self.time_zone is None:
            raise ValueError("time_zone is required when date_time is set")
        return self


class CalendarReminder(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    method: ReminderMethod = ReminderMethod.POPUP
    minutes_before_start: int = Field(
        ge=0,
        le=40320,
        validation_alias=AliasChoices("minutes_before_start", "minutes"),
        description="Minutes before the event start; Google's max is 4 weeks.",
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
    recurrence: list[str] | None = Field(
        default=None,
        min_length=1,
        description="RFC 5545 RRULE content lines, one per item (for example "
        "'RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12'). DTSTART and DTEND come from start/end. "
        "COUNT includes the first occurrence. Only RRULE is currently supported.",
    )

    @model_validator(mode="after")
    def _validate_recurrence(self) -> CreateCalendarEventArgs:
        if self.recurrence is None:
            return self
        for line in self.recurrence:
            if not line or line != line.strip() or "\n" in line or "\r" in line:
                raise ValueError("each recurrence item must be one non-empty unfolded content line")
            if not line.startswith("RRULE:"):
                raise ValueError("only RRULE recurrence lines are supported")
            component_names = {part.split("=", 1)[0].upper() for part in line.removeprefix("RRULE:").split(";")}
            if {"COUNT", "UNTIL"} <= component_names:
                raise ValueError("RRULE cannot contain both COUNT and UNTIL")
        try:
            rrulestr("\n".join(self.recurrence), dtstart=_event_dtstart(self.start), forceset=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid RRULE recurrence: {exc}") from exc
        return self


def _event_dtstart(value: EventDateTime) -> dt.datetime:
    if value.date is not None:
        return dt.datetime.combine(dt.date.fromisoformat(value.date), dt.time.min)
    assert value.date_time is not None
    parsed = dt.datetime.fromisoformat(value.date_time)
    assert value.time_zone is not None
    try:
        zone = ZoneInfo(value.time_zone)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown IANA time zone: {value.time_zone}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


class CalendarEventAttendee(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    email: str
    display_name: str | None = None
    response_status: str | None = None
    optional: bool = False
    organizer: bool = False
    self: bool = False


class CalendarEventOrganizer(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    email: str | None = None
    display_name: str | None = None
    self: bool = False


class CalendarEventReminders(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    use_default: bool = True
    overrides: list[CalendarReminder] = Field(default_factory=list)


class CalendarEvent(BaseModel):
    """Focused, stable event projection shared by create/get/list/instances."""

    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(validation_alias="id")
    etag: str | None = None
    status: str | None = None
    i_cal_uid: str | None = Field(default=None, validation_alias="iCalUID")
    created: str | None = None
    updated: str | None = None
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: EventDateTime | None = None
    end: EventDateTime | None = None
    recurrence: list[str] = Field(default_factory=list)
    recurring_event_id: str | None = Field(default=None, validation_alias="recurringEventId")
    original_start_time: EventDateTime | None = Field(default=None, validation_alias="originalStartTime")
    organizer: CalendarEventOrganizer | None = None
    attendees: list[CalendarEventAttendee] = Field(default_factory=list)
    reminders: CalendarEventReminders | None = None
    html_link: str | None = Field(
        default=None, validation_alias="htmlLink", description="Link to the event in Google Calendar."
    )


class CalendarEventsPage(BaseModel):
    """Focused page returned by list_events and list_event_instances."""

    model_config = ConfigDict(populate_by_name=True)

    events: list[CalendarEvent] = Field(default_factory=list, validation_alias="items")
    next_page_token: str | None = Field(default=None, validation_alias="nextPageToken")


class ListCalendarEventsArgs(BaseModel):
    calendar_id: str = "primary"
    time_min: str | None = Field(default=None, description="RFC3339 lower bound for event end time.")
    time_max: str | None = Field(default=None, description="RFC3339 upper bound for event start time.")
    query: str | None = Field(default=None, description="Free-text search query.")
    expand_recurring: bool = Field(
        default=False, description="Expand recurring series into instances instead of returning series masters."
    )
    max_results: int = Field(default=50, ge=1, le=250)
    page_token: str | None = None


class ListCalendarEventInstancesArgs(BaseModel):
    recurring_event_id: str
    calendar_id: str = "primary"
    time_min: str | None = Field(default=None, description="RFC3339 lower bound for instance end time.")
    time_max: str | None = Field(default=None, description="RFC3339 upper bound for instance start time.")
    max_results: int = Field(default=50, ge=1, le=250)
    page_token: str | None = None


class _CalendarListEntry(BaseModel):
    """The slice of the Calendar API's `calendarList` entry the approval UI needs."""

    model_config = ConfigDict(populate_by_name=True)

    summary: str
    # The operator's own name for the calendar, when they've renamed it; wins over `summary`.
    summary_override: str | None = Field(default=None, validation_alias="summaryOverride")


class CalendarSummary(BaseModel):
    """A calendar's display name plus a link into the Google Calendar web UI, for rendering a
    pending `create_event` approval — the tool call carries only the `calendar_id`, so
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
    recurrence: list[str] | None = None


class CalendarToolsClient:
    """Focused event reads and creation over a raw Calendar service. Calendar-name resolution is a
    rendering-support read, not a tool op — see the module-level `resolve_calendar_summary`."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def create_event(self, args: CreateCalendarEventArgs) -> CalendarEvent:
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
            recurrence=args.recurrence,
        )
        created = (
            self.service.events()
            .insert(calendarId=args.calendar_id, body=body.model_dump(by_alias=True, exclude_none=True))
            .execute()
        )
        return CalendarEvent.model_validate(created)

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        event = self.service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return CalendarEvent.model_validate(event)

    def list_events(self, args: ListCalendarEventsArgs) -> CalendarEventsPage:
        params: dict[str, Any] = {
            "calendarId": args.calendar_id,
            "singleEvents": args.expand_recurring,
            "maxResults": args.max_results,
        }
        if args.time_min is not None:
            params["timeMin"] = args.time_min
        if args.time_max is not None:
            params["timeMax"] = args.time_max
        if args.query is not None:
            params["q"] = args.query
        if args.page_token is not None:
            params["pageToken"] = args.page_token
        return CalendarEventsPage.model_validate(self.service.events().list(**params).execute())

    def list_event_instances(self, args: ListCalendarEventInstancesArgs) -> CalendarEventsPage:
        params: dict[str, Any] = {
            "calendarId": args.calendar_id,
            "eventId": args.recurring_event_id,
            "maxResults": args.max_results,
        }
        if args.time_min is not None:
            params["timeMin"] = args.time_min
        if args.time_max is not None:
            params["timeMax"] = args.time_max
        if args.page_token is not None:
            params["pageToken"] = args.page_token
        return CalendarEventsPage.model_validate(self.service.events().instances(**params).execute())


def resolve_calendar_summary(service: Any, calendar_id: str) -> CalendarSummary:
    """The calendar's display name + a Google Calendar link, for rendering a pending
    `create_event` approval. `calendarList` carries the operator's own naming
    (`summaryOverride`), which the mounted token's `calendar.readonly` scope covers — a
    rendering read, not a tool the agent invokes, so a free function over the raw service."""
    entry = _CalendarListEntry.model_validate(service.calendarList().get(calendarId=calendar_id).execute())
    cid = base64.b64encode(calendar_id.encode()).decode().rstrip("=")
    return CalendarSummary(
        calendar_id=calendar_id,
        summary=entry.summary_override or entry.summary,
        html_link=_CALENDAR_URL.format(cid=cid),
    )
