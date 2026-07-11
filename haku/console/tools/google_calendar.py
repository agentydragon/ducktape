"""haku-console's in-process `google_calendar` MCP server.

Google Calendar event creation behind haku-console's operator-approval queue. Built as a
real `FastMCP` server attached via an in-memory transport (see `gmail.py` for the pattern),
so the whole approval/audit/CSRF/reflection pipeline in `mcp_approval.py` runs unchanged.
Registered as MCP server id `google_calendar` in `cluster/k8s/haku/console/config.yaml` (no
`server_url`). Shares the `haku_console_google` Airlock token with the `gmail` server. See
`haku/docs/security.md` for the credential/consent model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastmcp import FastMCP
from googleapiclient.discovery import build
from pydantic import BaseModel, Field

from gmail_api.service import credentials_from_token_dir
from haku.console.tools.google_calendar_client import (
    CalendarReminder,
    CalendarSummary,
    CalendarToolsClient,
    CreateCalendarEventArgs,
    CreateCalendarEventResult,
    EventDateTime,
    resolve_calendar_summary,
)

GOOGLE_CALENDAR_SERVER_ID = "google_calendar"

# The one write scope this tool needs. The mounted `haku_console_google` Airlock token
# (shared with the `gmail` server) carries this plus every other scope in the grant;
# requesting a subset here is harmless — the externally-rotated access token already holds
# whatever Airlock granted. See cluster/k8s/haku/console/README.md.
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_SCOPES = [CALENDAR_EVENTS_SCOPE]


# Module-level, not local to build_mcp(): `from __future__ import annotations` makes every
# tool parameter annotation a string, resolved by pydantic against this module's globals at
# decoration time — a name only local to build_mcp() would raise NameError there.
_RemindersAnn = Annotated[
    list[CalendarReminder] | None,
    Field(default=None, description="Overrides the calendar's default reminders. Omit to use the calendar default."),
]
_AttendeesAnn = Annotated[list[str] | None, Field(default=None, description="Attendee email addresses to invite.")]


def build_mcp(calendar: CalendarToolsClient) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=GOOGLE_CALENDAR_SERVER_ID,
        instructions="Privileged Google Calendar tools. Every call is gated by haku-console's ordinary "
        "operator-approval queue — there is no autonomous path.",
    )

    @mcp.tool
    async def create_calendar_event(
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
    ) -> CreateCalendarEventResult:
        """Create a Google Calendar event, optionally with custom reminders and attendees."""
        args = CreateCalendarEventArgs(
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            calendar_id=calendar_id,
            reminders=reminders or [],
            attendees=attendees or [],
        )
        return calendar.create_event(args)

    return mcp


def build_calendar_client(token_dir: Path) -> CalendarToolsClient:
    creds = credentials_from_token_dir(token_dir, CALENDAR_SCOPES)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False, static_discovery=True)
    return CalendarToolsClient(service)


def _calendar_client(request: Request) -> CalendarToolsClient:
    client = request.app.state.calendar_client
    if client is None:
        raise HTTPException(status_code=503, detail="Calendar tools are not configured (google_token_dir unset)")
    return cast(CalendarToolsClient, client)


CalendarClientDep = Annotated[CalendarToolsClient, Depends(_calendar_client)]

router = APIRouter(prefix="/api/google-calendar", tags=["google_calendar"])


@router.get("/calendar-summary")
async def calendar_summary(calendar: CalendarClientDep, calendar_id: Annotated[str, Query()]) -> CalendarSummary:
    """Live display-name + Google Calendar link for a calendar id, for rendering a pending
    `create_calendar_event` approval — the tool call only carries the id, so the approval UI
    resolves the human-readable name here. A plain HTTP read, not an MCP tool."""
    return resolve_calendar_summary(calendar.service, calendar_id)


class CalendarToolArgumentExamples(BaseModel):
    """Registers `create_calendar_event`'s argument model in the OpenAPI schema so the frontend
    gets both the runtime Zod validator and the inferred TS type from generated
    `api/schema.zod.ts` (see `tool_previews/google_calendar.tsx`). The value is a placeholder: nothing
    reads this endpoint's response, only `export_schema.py`'s static trace of it needs to exist."""

    create_calendar_event: CreateCalendarEventArgs


@router.get("/tool-argument-schema-examples")
async def calendar_tool_argument_schema_examples() -> CalendarToolArgumentExamples:
    return CalendarToolArgumentExamples(
        create_calendar_event=CreateCalendarEventArgs(
            summary="Example event", start=EventDateTime(date="2026-01-01"), end=EventDateTime(date="2026-01-02")
        )
    )
