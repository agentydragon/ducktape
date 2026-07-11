"""haku-console's in-process `gmail` MCP server.

Gmail read + write tools behind haku-console's operator-approval queue. The reads mirror
Gmail's REST API (they return `gmail_api` resource models verbatim); the writes create
drafts, change thread labels, and manage labels. Every call travels through the console's
approval and audit pipeline; reviewed policy may auto-approve existing label tools.

Built as a real `FastMCP` server and attached to `McpToolExecutor`/`McpMetadataProvider`
as an **in-process** transport (`fastmcp.client.Client` accepts a `FastMCP` instance
directly), so the whole approval/audit/CSRF/reflection pipeline in `mcp_approval.py` runs
unchanged; only the transport differs from a remote server's. Registered as MCP server id
`gmail` in `cluster/k8s/haku/console/config.yaml` (no `server_url`). Shares the
`haku_console_google` Airlock token with the `google_calendar` server. See
`haku/docs/security.md` for the credential/consent model, and `haku/console/TODO.md` for
Gmail API affordances not yet exposed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastmcp import FastMCP
from googleapiclient.discovery import build
from pydantic import BaseModel, Field

from gmail_api.labels import (
    CreateLabelRequest,
    GmailLabel,
    LabelListVisibility,
    LabelsListResponse,
    MessageListVisibility,
    PatchLabelRequest,
)
from gmail_api.messages import Draft, Message, MessageFormat, Thread, ThreadFormat, ThreadsListResponse
from gmail_api.service import credentials_from_token_dir
from haku.console.tools.gmail_client import (
    BatchModifyGmailThreadLabelsArgs,
    BatchModifyGmailThreadLabelsResult,
    CreateGmailDraftArgs,
    GmailThreadPreviewsResponse,
    GmailToolsClient,
    SearchThreadsArgs,
    preview_gmail_threads,
)

GMAIL_SERVER_ID = "gmail"

# The write scopes the label/draft tools need. `gmail.modify` also covers every read the
# search/get tools do, so no read-only scope is required. The mounted `haku_console_google`
# Airlock token (shared with the `google_calendar` server) carries these plus the read-only
# scopes; requesting a subset here is harmless — the externally-rotated access token already
# holds whatever Airlock granted. See cluster/k8s/haku/console/README.md.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SCOPES = [GMAIL_MODIFY_SCOPE, GMAIL_COMPOSE_SCOPE]


# Module-level, not local to build_mcp(): `from __future__ import annotations` makes every
# tool parameter annotation a string, resolved by pydantic against this module's globals at
# decoration time — a name only local to build_mcp() would raise NameError there.
_ThreadIdsAnn = Annotated[list[str], Field(min_length=1, description="Gmail thread IDs to modify in one batch.")]
_AddLabelsAnn = Annotated[
    list[str] | None, Field(default=None, description="Label names to add to every thread; created if new.")
]
_RemoveLabelsAnn = Annotated[
    list[str] | None, Field(default=None, description="Label names to remove from every thread; must exist.")
]


def build_mcp(gmail: GmailToolsClient) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=GMAIL_SERVER_ID,
        instructions="Privileged Gmail tools. Reads (search/get threads, messages, labels) mirror Gmail's REST "
        "API and return its resource shapes verbatim; writes create drafts, change thread labels, and manage "
        "labels. Calls go through haku-console's approval and audit pipeline; reviewed policy may auto-approve "
        "standing label-read authority and mutations confined to haku/.",
    )

    @mcp.tool
    async def threads_list(
        query: Annotated[
            str,
            Field(
                description="Gmail search query, same syntax as the Gmail search box "
                "(e.g. 'from:alice after:2026/01/01 is:unread')."
            ),
        ],
        max_results: Annotated[int, Field(ge=1, le=500, description="Maximum threads per page.")] = 25,
        page_token: Annotated[
            str | None, Field(default=None, description="`next_page_token` from a previous response; omit for page 1.")
        ] = None,
    ) -> ThreadsListResponse:
        """Search Gmail threads (mirrors users.threads.list): thread stubs plus `next_page_token` for paging."""
        return gmail.threads_list(SearchThreadsArgs(query=query, max_results=max_results, page_token=page_token))

    @mcp.tool
    async def threads_get(
        thread_id: Annotated[str, Field(description="Gmail thread ID.")], format: ThreadFormat = ThreadFormat.FULL
    ) -> Thread:
        """Fetch a Gmail thread and its messages (mirrors users.threads.get). `format` sets the detail level."""
        return gmail.threads_get(thread_id, format)

    @mcp.tool
    async def messages_get(
        message_id: Annotated[str, Field(description="Gmail message ID.")], format: MessageFormat = MessageFormat.FULL
    ) -> Message:
        """Fetch a Gmail message (mirrors users.messages.get). `format` sets the detail level (raw = full RFC 2822)."""
        return gmail.messages_get(message_id, format)

    @mcp.tool
    async def labels_list() -> LabelsListResponse:
        """List every Gmail label (mirrors users.labels.list)."""
        return gmail.labels_list()

    @mcp.tool
    async def labels_get(label_id: Annotated[str, Field(description="Gmail label ID.")]) -> GmailLabel:
        """Fetch one Gmail label including its message/thread counts (mirrors users.labels.get)."""
        return gmail.labels_get(label_id)

    @mcp.tool
    async def threads_batch_modify(
        thread_ids: _ThreadIdsAnn, add: _AddLabelsAnn = None, remove: _RemoveLabelsAnn = None
    ) -> BatchModifyGmailThreadLabelsResult:
        """Add and/or remove labels (by name; missing add-labels are created) across a batch of threads."""
        args = BatchModifyGmailThreadLabelsArgs(thread_ids=thread_ids, add=add or [], remove=remove or [])
        return gmail.threads_batch_modify(args)

    @mcp.tool
    async def drafts_create(
        to: Annotated[list[str], Field(min_length=1, description="Recipient email addresses.")],
        subject: str,
        body: Annotated[str, Field(description="Plain-text message body.")],
        cc: Annotated[list[str] | None, Field(default=None)] = None,
        thread_id: Annotated[
            str | None, Field(default=None, description="Existing Gmail thread ID to draft a reply within.")
        ] = None,
    ) -> Draft:
        """Create a Gmail draft (never sent automatically — the operator sends it from Gmail)."""
        args = CreateGmailDraftArgs(to=to, subject=subject, body=body, cc=cc or [], thread_id=thread_id)
        return gmail.drafts_create(args)

    @mcp.tool
    async def labels_create(
        name: Annotated[str, Field(description="New label name; use '/' to nest (e.g. 'receipts/amazon').")],
        label_list_visibility: LabelListVisibility = LabelListVisibility.LABEL_SHOW,
        message_list_visibility: MessageListVisibility = MessageListVisibility.SHOW,
    ) -> GmailLabel:
        """Create a Gmail label (mirrors users.labels.create)."""
        return gmail.labels_create(
            CreateLabelRequest(
                name=name, label_list_visibility=label_list_visibility, message_list_visibility=message_list_visibility
            )
        )

    @mcp.tool
    async def labels_patch(
        label_id: Annotated[str, Field(description="ID of the label to update (from labels_list/labels_get).")],
        name: Annotated[str | None, Field(default=None, description="New label name.")] = None,
        label_list_visibility: LabelListVisibility | None = None,
        message_list_visibility: MessageListVisibility | None = None,
    ) -> GmailLabel:
        """Update a label's name and/or visibility (mirrors users.labels.patch; only the fields you set change)."""
        return gmail.labels_patch(
            label_id,
            PatchLabelRequest(
                name=name, label_list_visibility=label_list_visibility, message_list_visibility=message_list_visibility
            ),
        )

    @mcp.tool
    async def labels_delete(
        label_id: Annotated[str, Field(description="ID of the label to delete; it is removed from every thread.")],
    ) -> None:
        """Delete a Gmail label (mirrors users.labels.delete; drops it from every thread it was on)."""
        gmail.labels_delete(label_id)

    return mcp


def build_gmail_client(token_dir: Path) -> GmailToolsClient:
    creds = credentials_from_token_dir(token_dir, GMAIL_SCOPES)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False, static_discovery=True)
    return GmailToolsClient(service)


def _gmail_client(request: Request) -> GmailToolsClient:
    client = request.app.state.gmail_client
    if client is None:
        raise HTTPException(status_code=503, detail="Gmail tools are not configured (google_token_dir unset)")
    return cast(GmailToolsClient, client)


GmailClientDep = Annotated[GmailToolsClient, Depends(_gmail_client)]

router = APIRouter(prefix="/api/gmail", tags=["gmail"])


@router.get("/thread-previews")
async def gmail_thread_previews(
    gmail: GmailClientDep, thread_id: Annotated[list[str], Query()]
) -> GmailThreadPreviewsResponse:
    """Live subject/snippet/current-labels lookup, for rendering a pending or past
    `threads_batch_modify` approval — the tool call itself only carries thread IDs, so the
    approval UI resolves display text here rather than trusting caller-supplied text it can't
    verify. A plain HTTP read, not an MCP tool — outside `build_mcp`'s surface."""
    return GmailThreadPreviewsResponse(threads=preview_gmail_threads(gmail.service, thread_id))


class GmailToolArgumentExamples(BaseModel):
    """Registers the write tools' argument models in the OpenAPI schema so the frontend gets
    both the runtime Zod validator and the inferred TS type from generated `api/schema.zod.ts`
    (see `tool_previews/gmail.tsx`). The read tools are omitted — their arguments (a query
    string, an id, a format enum) render fine in the approval drawer's generic argument view.
    The values are placeholders: nothing reads this endpoint's response, only
    `export_schema.py`'s static trace of it needs to exist for these models to reach the schema."""

    threads_batch_modify: BatchModifyGmailThreadLabelsArgs
    drafts_create: CreateGmailDraftArgs


@router.get("/tool-argument-schema-examples")
async def gmail_tool_argument_schema_examples() -> GmailToolArgumentExamples:
    return GmailToolArgumentExamples(
        threads_batch_modify=BatchModifyGmailThreadLabelsArgs(thread_ids=["example-thread-id"], add=["example"]),
        drafts_create=CreateGmailDraftArgs(to=["example@example.com"], subject="Example", body="Example"),
    )
