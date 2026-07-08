"""Tool-facing models for haku-console's privileged Google tools.

Argument models double as each tool's `input_schema` (`.model_json_schema()`), so field
descriptions here are what the operator-approval UI and the calling agent both see.
"""

from __future__ import annotations

from typing import Literal

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


class GmailLabelRef(BaseModel):
    name: str
    id: str


class BatchModifyGmailThreadLabelsArgs(BaseModel):
    """Add and/or remove Gmail labels across a batch of threads in one call."""

    thread_ids: list[str] = Field(min_length=1, description="Gmail thread IDs to modify in one batch.")
    add: list[str] = Field(default_factory=list, description="Label names to add to every thread; created if new.")
    remove: list[str] = Field(default_factory=list, description="Label names to remove from every thread; must exist.")

    @model_validator(mode="after")
    def _at_least_one_change(self) -> BatchModifyGmailThreadLabelsArgs:
        if not self.add and not self.remove:
            raise ValueError("must specify at least one label in add or remove")
        if overlap := set(self.add) & set(self.remove):
            raise ValueError(f"label(s) {sorted(overlap)} cannot be both added and removed in the same call")
        return self


class BatchModifyGmailThreadLabelsResult(BaseModel):
    added: list[GmailLabelRef]
    removed: list[GmailLabelRef]
    thread_count: int


class CreateGmailDraftArgs(BaseModel):
    """Create a Gmail draft (never sent automatically — the operator sends it from Gmail)."""

    to: list[str] = Field(min_length=1, description="Recipient email addresses.")
    subject: str
    body: str = Field(description="Plain-text message body.")
    cc: list[str] = Field(default_factory=list)
    thread_id: str | None = Field(default=None, description="Existing Gmail thread ID to draft a reply within.")


class CreateGmailDraftResult(BaseModel):
    draft_id: str
    message_id: str


class GmailThreadPreview(BaseModel):
    subject: str
    snippet: str
    current_label_names: list[str]
    gmail_url: str = Field(description="Link to the thread in the Gmail web UI.")


class GmailThreadPreviewsResponse(BaseModel):
    threads: dict[str, GmailThreadPreview] = Field(
        description="Keyed by thread_id; a requested id absent from the map was inaccessible (deleted, wrong account, …)."
    )
