"""haku-console's privileged Google tools: calendar event creation, batch Gmail thread
label changes, and Gmail draft creation.

Built as a real `FastMCP` server (exactly like a standalone MCP server would be), then
attached to `McpToolExecutor`/`McpMetadataProvider` as an **in-process** transport —
`fastmcp.client.Client` accepts a `FastMCP` instance directly (an in-memory
`FastMCPTransport`), so the whole approval/audit/CSRF/reflection pipeline in
`mcp_approval.py` runs completely unchanged; only the transport differs from a remote
server's. See `haku/docs/security.md` for the credential/consent model.

Registered as MCP server id `google` in `cluster/k8s/haku/console/config.yaml` (no
`server_url` — resolved via the `in_process_servers` registry `create_app` builds
instead); the config entry is otherwise ordinary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastmcp import FastMCP
from googleapiclient.discovery import build
from pydantic import BaseModel, Field

from gmail_api.service import credentials_from_token_dir
from haku.console.tools.google_calendar import (
    CalendarReminder,
    CalendarToolsClient,
    CreateCalendarEventArgs,
    CreateCalendarEventResult,
    EventDateTime,
)
from haku.console.tools.google_gmail import (
    BatchModifyGmailThreadLabelsArgs,
    BatchModifyGmailThreadLabelsResult,
    CreateGmailDraftArgs,
    CreateGmailDraftResult,
    GmailThreadPreviewsResponse,
    GmailToolsClient,
    preview_gmail_threads,
)

GOOGLE_SERVER_ID = "google"

# The write scopes these three tools need. The actual Airlock-issued token (see
# cluster/k8s/agents/airlock/config.yaml -> `haku_console_google`) carries these plus
# every read-only scope the `google` provider does, so a future read feature here
# doesn't need a second consent round-trip — but this module only ever calls the write
# APIs, kept separate from Haku's own read-only google-access-token /
# gmail-labeling's gmail.modify-only token (narrower than the third-party
# google-workspace-mcp's all-of-Workspace grant would be).
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
HAKU_CONSOLE_GOOGLE_SCOPES = [CALENDAR_EVENTS_SCOPE, GMAIL_MODIFY_SCOPE, GMAIL_COMPOSE_SCOPE]


# Module-level, not local to build_mcp(): `from __future__ import annotations` makes
# every tool parameter annotation below a string, resolved by pydantic against the
# function's module globals at decoration time — a name only local to build_mcp()
# would raise NameError there.
_RemindersAnn = Annotated[
    list[CalendarReminder] | None,
    Field(default=None, description="Overrides the calendar's default reminders. Omit to use the calendar default."),
]
_AttendeesAnn = Annotated[list[str] | None, Field(default=None, description="Attendee email addresses to invite.")]
_ThreadIdsAnn = Annotated[list[str], Field(min_length=1, description="Gmail thread IDs to modify in one batch.")]
_AddLabelsAnn = Annotated[
    list[str] | None, Field(default=None, description="Label names to add to every thread; created if new.")
]
_RemoveLabelsAnn = Annotated[
    list[str] | None, Field(default=None, description="Label names to remove from every thread; must exist.")
]


def build_mcp(gmail: GmailToolsClient, calendar: CalendarToolsClient) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=GOOGLE_SERVER_ID,
        instructions="Privileged Google Calendar/Gmail tools. Every call is gated by haku-console's "
        "ordinary operator-approval queue — there is no autonomous path to any of these.",
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

    @mcp.tool
    async def batch_modify_gmail_thread_labels(
        thread_ids: _ThreadIdsAnn, add: _AddLabelsAnn = None, remove: _RemoveLabelsAnn = None
    ) -> BatchModifyGmailThreadLabelsResult:
        """Add and/or remove Gmail labels across a batch of threads in one call."""
        args = BatchModifyGmailThreadLabelsArgs(thread_ids=thread_ids, add=add or [], remove=remove or [])
        return gmail.batch_modify_thread_labels(args)

    @mcp.tool
    async def create_gmail_draft(
        to: Annotated[list[str], Field(min_length=1, description="Recipient email addresses.")],
        subject: str,
        body: Annotated[str, Field(description="Plain-text message body.")],
        cc: Annotated[list[str] | None, Field(default=None)] = None,
        thread_id: Annotated[
            str | None, Field(default=None, description="Existing Gmail thread ID to draft a reply within.")
        ] = None,
    ) -> CreateGmailDraftResult:
        """Create a Gmail draft (never sent automatically — the operator sends it from Gmail)."""
        args = CreateGmailDraftArgs(to=to, subject=subject, body=body, cc=cc or [], thread_id=thread_id)
        return gmail.create_draft(args)

    return mcp


def build_tool_clients(token_dir: Path) -> tuple[GmailToolsClient, CalendarToolsClient]:
    creds = credentials_from_token_dir(token_dir, HAKU_CONSOLE_GOOGLE_SCOPES)
    gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False, static_discovery=True)
    calendar_service = build("calendar", "v3", credentials=creds, cache_discovery=False, static_discovery=True)
    return GmailToolsClient(gmail_service), CalendarToolsClient(calendar_service)


def build_app_mcp(token_dir: Path) -> FastMCP:
    """Build the in-process `google` FastMCP server from a mounted token directory —
    what `create_app` registers into `McpToolExecutor`/`McpMetadataProvider`."""
    gmail, calendar = build_tool_clients(token_dir)
    return build_mcp(gmail, calendar)


def _gmail_client(request: Request) -> GmailToolsClient:
    client = request.app.state.google_gmail_client
    if client is None:
        raise HTTPException(status_code=503, detail="Google tools are not configured (google_token_dir unset)")
    return cast(GmailToolsClient, client)


GmailClientDep = Annotated[GmailToolsClient, Depends(_gmail_client)]

router = APIRouter(prefix="/api/google", tags=["google"])


@router.get("/gmail/thread-previews")
async def gmail_thread_previews(
    gmail: GmailClientDep, thread_id: Annotated[list[str], Query()]
) -> GmailThreadPreviewsResponse:
    """Live subject/snippet/current-labels lookup, for rendering a pending or past
    `batch_modify_gmail_thread_labels` approval — the tool call itself only carries thread
    IDs, so the approval UI resolves display text here rather than trusting caller-supplied
    text it can't verify. A plain HTTP read, not an MCP tool — outside `build_mcp`'s surface."""
    return GmailThreadPreviewsResponse(threads=preview_gmail_threads(gmail.service, thread_id))


class GoogleToolArgumentExamples(BaseModel):
    """Registers the three MCP tools' argument models in the OpenAPI schema so the
    frontend gets both the runtime Zod validator and the inferred TS type from
    generated `api/schema.zod.ts` (see `google_tool_previews.tsx`) instead of hand-
    declaring parallel TypeScript interfaces and shape checks — the same technique
    `GmailThreadPreview` above uses. The values are placeholders: nothing reads this
    endpoint's response, only `export_schema.py`'s static trace of it needs to exist
    for these models to reach the schema."""

    create_calendar_event: CreateCalendarEventArgs
    batch_modify_gmail_thread_labels: BatchModifyGmailThreadLabelsArgs
    create_gmail_draft: CreateGmailDraftArgs


@router.get("/tool-argument-schema-examples")
async def google_tool_argument_schema_examples() -> GoogleToolArgumentExamples:
    return GoogleToolArgumentExamples(
        create_calendar_event=CreateCalendarEventArgs(
            summary="Example event", start=EventDateTime(date="2026-01-01"), end=EventDateTime(date="2026-01-02")
        ),
        batch_modify_gmail_thread_labels=BatchModifyGmailThreadLabelsArgs(
            thread_ids=["example-thread-id"], add=["example-label"]
        ),
        create_gmail_draft=CreateGmailDraftArgs(to=["example@example.com"], subject="Example", body="Example"),
    )
