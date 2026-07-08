"""haku-console's privileged Google tools: calendar event creation, batch Gmail thread
label changes, and Gmail draft creation — behind the ordinary MCP-approval queue
(`mcp_approval.py`), but implemented natively in-process rather than as a call to a
remote MCP server. See `haku/docs/security.md` for the credential/consent model.

Registered as MCP server id `google` with `native: true` in `cluster/k8s/haku/console/
config.yaml`; `create_app` builds one `GoogleToolProvider` from `Settings.google_token_dir`
and hands it to both `McpToolExecutor` and `McpMetadataProvider`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from googleapiclient.discovery import build
from pydantic import ValidationError

from gmail_api.service import credentials_from_token_dir
from haku.console.google_calendar_tools import CalendarToolsClient
from haku.console.google_gmail_tools import GmailToolsClient
from haku.console.google_tools_models import (
    BatchModifyGmailThreadLabelsArgs,
    CreateCalendarEventArgs,
    CreateGmailDraftArgs,
    GmailThreadPreviewsResponse,
)
from haku.console.mcp_approval import AliveServerMetadata, AliveToolMetadata, ServerMetadata

GOOGLE_SERVER_ID = "google"

# One Airlock-issued token, scoped for exactly these three tools — narrower than the
# third-party google-workspace-mcp's all-of-Workspace grant, and kept separate from
# Haku's own read-only google-access-token / gmail-labeling's gmail.modify-only token
# (see cluster/k8s/agents/airlock/config.yaml -> `console_google`).
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
CONSOLE_GOOGLE_SCOPES = [CALENDAR_EVENTS_SCOPE, GMAIL_MODIFY_SCOPE, GMAIL_COMPOSE_SCOPE]

_TOOLS = {
    "create_calendar_event": CreateCalendarEventArgs,
    "batch_modify_gmail_thread_labels": BatchModifyGmailThreadLabelsArgs,
    "create_gmail_draft": CreateGmailDraftArgs,
}


class GoogleToolProvider:
    """The native `NativeToolProvider` for MCP server id `google` (see `mcp_approval.py`)."""

    def __init__(self, gmail: GmailToolsClient, calendar: CalendarToolsClient) -> None:
        self._gmail = gmail
        self._calendar = calendar

    @classmethod
    def from_token_dir(cls, token_dir: Path) -> GoogleToolProvider:
        creds = credentials_from_token_dir(token_dir, CONSOLE_GOOGLE_SCOPES)
        gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False, static_discovery=True)
        calendar_service = build("calendar", "v3", credentials=creds, cache_discovery=False, static_discovery=True)
        return cls(GmailToolsClient(gmail_service), CalendarToolsClient(calendar_service))

    async def metadata(self) -> ServerMetadata:
        return AliveServerMetadata(
            server_id=GOOGLE_SERVER_ID,
            title="Google (calendar + gmail, privileged)",
            tools=[
                AliveToolMetadata(name=name, description=model.__doc__, input_schema=model.model_json_schema())
                for name, model in _TOOLS.items()
            ],
        )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        model = _TOOLS.get(tool_name)
        if model is None:
            raise ValueError(f"unknown tool {tool_name!r} for server {GOOGLE_SERVER_ID!r}")
        try:
            args = model.model_validate(arguments)
        except ValidationError as e:
            raise ValueError(f"invalid arguments for {tool_name}: {e}") from e
        if isinstance(args, CreateCalendarEventArgs):
            return self._calendar.create_event(args).model_dump(mode="json")
        if isinstance(args, BatchModifyGmailThreadLabelsArgs):
            return self._gmail.batch_modify_thread_labels(args).model_dump(mode="json")
        if isinstance(args, CreateGmailDraftArgs):
            return self._gmail.create_draft(args).model_dump(mode="json")
        raise AssertionError(f"unreachable: unhandled args type {type(args)}")

    def preview_threads(self, thread_ids: list[str]) -> GmailThreadPreviewsResponse:
        return GmailThreadPreviewsResponse(threads=self._gmail.preview_threads(thread_ids))


def _google_tools(request: Request) -> GoogleToolProvider:
    provider = request.app.state.google_tool_provider
    if provider is None:
        raise HTTPException(status_code=503, detail="Google tools are not configured (google_token_dir unset)")
    return cast(GoogleToolProvider, provider)


GoogleToolsDep = Annotated[GoogleToolProvider, Depends(_google_tools)]

router = APIRouter(prefix="/api/google", tags=["google"])


@router.get("/gmail/thread-previews")
async def gmail_thread_previews(
    provider: GoogleToolsDep, thread_id: Annotated[list[str], Query()]
) -> GmailThreadPreviewsResponse:
    """Live subject/snippet/current-labels lookup, for rendering a pending or past
    `batch_modify_gmail_thread_labels` approval — the tool call itself only carries thread
    IDs, so the approval UI resolves display text here rather than trusting caller-supplied
    text it can't verify."""
    return provider.preview_threads(thread_id)
