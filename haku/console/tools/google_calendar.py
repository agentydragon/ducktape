"""haku-console's in-process `google_calendar` MCP server.

Google Calendar event reads and creation behind haku-console's approval policy. Built as a
real `FastMCP` server attached via an in-memory transport (see `gmail.py` for the pattern),
so the application service's approval/audit lifecycle and the HTTP adapter's CSRF/reflection
behavior run unchanged.
Registered as MCP server id `google_calendar` in `cluster/k8s/haku/console/config.yaml` (no
`server_url`). Executes as the acting Operator's own Google account via the console's per-Operator
connection store (`provider_connection.py`, `provider_connection: google`) — the same store the
`gmail` server uses. See `haku/docs/security.md` for the credential/consent model.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from haku.console.tools.google_calendar_client import (
    CalendarEvent,
    CalendarEventsPage,
    CalendarReminder,
    CalendarToolsClient,
    CreateCalendarEventArgs,
    EventDateTime,
    ListCalendarEventInstancesArgs,
    ListCalendarEventsArgs,
)
from haku.console.tools.google_service import build_google_api_service

GOOGLE_CALENDAR_SERVER_ID = "google_calendar"
# Reads only fetch; advertise read-only so clients (claude.ai) skip per-call approval prompts.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


# Module-level, not local to build_mcp(): `from __future__ import annotations` makes every
# tool parameter annotation a string, resolved by pydantic against this module's globals at
# decoration time — a name only local to build_mcp() would raise NameError there.
_RemindersAnn = Annotated[
    list[CalendarReminder] | None,
    Field(default=None, description="Overrides the calendar's default reminders. Omit to use the calendar default."),
]
_AttendeesAnn = Annotated[list[str] | None, Field(default=None, description="Attendee email addresses to invite.")]
_RecurrenceAnn = Annotated[
    list[str] | None,
    Field(
        default=None,
        min_length=1,
        description="RFC 5545 RRULE content lines, one per item (for example "
        "'RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12'). DTSTART and DTEND come from start/end. "
        "COUNT includes the first occurrence. Only RRULE is currently supported.",
    ),
]


def build_mcp(calendar: CalendarToolsClient) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=GOOGLE_CALENDAR_SERVER_ID,
        instructions="Privileged Google Calendar tools. Reads follow haku-console's reviewed standing "
        "policy; writes require the ordinary operator-approval queue.",
    )

    @mcp.tool
    async def create_event(
        summary: Annotated[str, Field(description="Event title.")],
        start: EventDateTime,
        end: EventDateTime,
        description: Annotated[str | None, Field(default=None, description="Event body text.")] = None,
        location: str | None = None,
        calendar_id: Annotated[
            str, Field(description="Target calendar; 'primary' is the operator's main calendar.")
        ] = "primary",
        reminders: _RemindersAnn = None,
        attendees: _AttendeesAnn = None,
        recurrence: _RecurrenceAnn = None,
    ) -> CalendarEvent:
        """Create an event or recurring series, optionally with reminders and attendees."""
        args = CreateCalendarEventArgs(
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            calendar_id=calendar_id,
            reminders=reminders or [],
            attendees=attendees or [],
            recurrence=recurrence,
        )
        return calendar.create_event(args)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_event(
        event_id: Annotated[str, Field(description="Google Calendar event or instance ID.")],
        calendar_id: Annotated[
            str, Field(description="Source calendar; 'primary' is the operator's main calendar.")
        ] = "primary",
    ) -> CalendarEvent:
        """Fetch one event, recurring-series master, or recurring-event instance."""
        return calendar.get_event(calendar_id, event_id)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_events(
        calendar_id: Annotated[
            str, Field(description="Source calendar; 'primary' is the operator's main calendar.")
        ] = "primary",
        time_min: Annotated[
            str | None, Field(default=None, description="RFC3339 lower bound for event end time.")
        ] = None,
        time_max: Annotated[
            str | None, Field(default=None, description="RFC3339 upper bound for event start time.")
        ] = None,
        query: Annotated[str | None, Field(default=None, description="Free-text search query.")] = None,
        expand_recurring: Annotated[
            bool, Field(description="Expand recurring series into instances instead of returning series masters.")
        ] = False,
        max_results: Annotated[int, Field(ge=1, le=250, description="Maximum events per page.")] = 50,
        page_token: Annotated[
            str | None, Field(default=None, description="`next_page_token` from a previous response.")
        ] = None,
    ) -> CalendarEventsPage:
        """List events, series masters, and exceptions, optionally expanding recurring instances."""
        return calendar.list_events(
            ListCalendarEventsArgs(
                calendar_id=calendar_id,
                time_min=time_min,
                time_max=time_max,
                query=query,
                expand_recurring=expand_recurring,
                max_results=max_results,
                page_token=page_token,
            )
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_event_instances(
        recurring_event_id: Annotated[str, Field(description="Recurring-series master event ID.")],
        calendar_id: Annotated[
            str, Field(description="Source calendar; 'primary' is the operator's main calendar.")
        ] = "primary",
        time_min: Annotated[
            str | None, Field(default=None, description="RFC3339 lower bound for instance end time.")
        ] = None,
        time_max: Annotated[
            str | None, Field(default=None, description="RFC3339 upper bound for instance start time.")
        ] = None,
        max_results: Annotated[int, Field(ge=1, le=250, description="Maximum instances per page.")] = 50,
        page_token: Annotated[
            str | None, Field(default=None, description="`next_page_token` from a previous response.")
        ] = None,
    ) -> CalendarEventsPage:
        """List the expanded occurrences and exceptions of one recurring series."""
        return calendar.list_event_instances(
            ListCalendarEventInstancesArgs(
                recurring_event_id=recurring_event_id,
                calendar_id=calendar_id,
                time_min=time_min,
                time_max=time_max,
                max_results=max_results,
                page_token=page_token,
            )
        )

    return mcp


def build_calendar_client_from_token(access_token: str | None) -> CalendarToolsClient:
    """Build the Calendar client for one call from the acting Operator's access token."""
    return CalendarToolsClient(build_google_api_service("calendar", "v3", access_token))
