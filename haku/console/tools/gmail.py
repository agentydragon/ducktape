"""haku-console's in-process `gmail` MCP server.

Gmail read + write tools behind haku-console's operator-approval queue. The **reads are generated
from Google's discovery doc** (`google_discovery.py`): they expose Google's native param names and
return its REST resource shapes verbatim. The **writes** stay hand-authored (shaped bodies / label
policy): create drafts, change thread labels, and manage labels/filters. Every call travels through
the console's approval and audit pipeline; reviewed policy may auto-approve reads + `haku/`-label
mutations.

Built as a real `FastMCP` server and attached to `McpServerClient`
as an **in-process** transport (`fastmcp.client.Client` accepts a `FastMCP` instance
directly), so the application service's approval/audit lifecycle and the HTTP adapter's
Origin/reflection behavior run unchanged; only the transport differs from a remote server's. Registered
as MCP server id `gmail` with an `in_process` backend in
`cluster/k8s/haku/console/config.yaml`. Executes as the acting Operator's own Google account,
resolving the config-bound `google_mail` operator connection from the console's connection store
(`provider_connection.py`). See
`haku/docs/security.md` for the credential/consent model, and `haku/console/TODO.md` for Gmail API
affordances not yet exposed.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gmail_api.filters import FilterAction, FilterCriteria, GmailFilter
from gmail_api.labels import (
    CreateLabelRequest,
    GmailLabel,
    LabelListVisibility,
    MessageListVisibility,
    PatchLabelRequest,
)
from gmail_api.messages import Draft
from haku.console.tools.gmail_client import (
    GMAIL_SERVER_ID,
    CreateGmailDraftArgs,
    GmailToolsClient,
    ModifyGmailThreadLabelsArgs,
    ModifyGmailThreadLabelsResult,
    UpdateGmailDraftArgs,
)
from haku.console.tools.google_discovery import GenTool, build_generated_tools
from haku.console.tools.google_service import build_google_api_service

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

# Read tools are generated from Google's discovery doc (schema + generic executor): they expose
# Google's native params/result shapes verbatim, pinning only `userId=me` (each call runs as the
# acting Operator's own account). Writes below stay hand-authored (shaped bodies / label policy).
# Auto-approval keys on these exact names (auto_approval.py).
_ME = {"userId": "me"}
# Reads only fetch. Advertise read-only so clients (claude.ai) group them as reads and skip
# per-call approval prompts. openWorldHint stays default (true): they reach the Operator's
# external Gmail mailbox — open world, unlike the console's own closed catalog.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_GMAIL_READ_TOOLS: list[GenTool] = [
    GenTool("gmail.users.threads.list", "threads_list", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.threads.get", "threads_get", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.messages.get", "messages_get", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.labels.list", "labels_list", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.labels.get", "labels_get", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.settings.filters.list", "filters_list", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.settings.filters.get", "filters_get", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.drafts.list", "drafts_list", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
    GenTool("gmail.users.drafts.get", "drafts_get", "gmail.v1", pin=_ME, annotations=_READ_ONLY),
]


def build_mcp(gmail: GmailToolsClient) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=GMAIL_SERVER_ID,
        strict_input_validation=True,
        instructions="Privileged Gmail tools. Reads (search/get threads, messages, labels, filters, drafts) mirror "
        "Gmail's REST API and return its resource shapes verbatim; writes create/update/delete drafts, change thread "
        "labels, manage labels, and manage filters. Calls go through haku-console's approval and audit pipeline; its "
        "reviewed decision may auto-approve standing read authority and label mutations confined to haku/.",
    )

    for tool in build_generated_tools(_GMAIL_READ_TOOLS, gmail.service):
        mcp.add_tool(tool)

    @mcp.tool
    async def filters_create(criteria: FilterCriteria, action: FilterAction) -> GmailFilter:
        """Create a Gmail filter (mirrors users.settings.filters.create). Both `criteria` and `action` are required."""
        return gmail.filters_create(criteria, action)

    @mcp.tool
    async def filters_delete(
        filter_id: Annotated[str, Field(description="ID of the filter to delete (from filters_list/filters_get).")],
    ) -> None:
        """Delete a Gmail filter (mirrors users.settings.filters.delete)."""
        gmail.filters_delete(filter_id)

    @mcp.tool
    async def drafts_update(
        draft_id: Annotated[str, Field(description="ID of the draft to replace (from drafts_list/drafts_get).")],
        to: Annotated[list[str], Field(min_length=1, description="Recipient email addresses.")],
        subject: str,
        body: Annotated[str, Field(description="Plain-text message body.")],
        cc: Annotated[list[str] | None, Field(default=None)] = None,
        bcc: Annotated[list[str] | None, Field(default=None, description="Bcc recipient email addresses.")] = None,
        thread_id: Annotated[
            str | None, Field(default=None, description="Existing Gmail thread ID to keep the draft within.")
        ] = None,
    ) -> Draft:
        """Replace a Gmail draft's message (mirrors users.drafts.update)."""
        args = UpdateGmailDraftArgs(
            draft_id=draft_id, to=to, subject=subject, body=body, cc=cc or [], bcc=bcc or [], thread_id=thread_id
        )
        return gmail.drafts_update(args)

    @mcp.tool
    async def drafts_delete(
        draft_id: Annotated[str, Field(description="ID of the draft to delete (from drafts_list/drafts_get).")],
    ) -> None:
        """Delete a Gmail draft (mirrors users.drafts.delete)."""
        gmail.drafts_delete(draft_id)

    @mcp.tool
    async def threads_modify_labels(
        thread_ids: _ThreadIdsAnn, add: _AddLabelsAnn = None, remove: _RemoveLabelsAnn = None
    ) -> ModifyGmailThreadLabelsResult:
        """Add and/or remove labels (by name; missing add-labels are created) across a batch of threads."""
        args = ModifyGmailThreadLabelsArgs(thread_ids=thread_ids, add=add or [], remove=remove or [])
        return gmail.threads_modify_labels(args)

    @mcp.tool
    async def drafts_create(
        to: Annotated[list[str], Field(min_length=1, description="Recipient email addresses.")],
        subject: str,
        body: Annotated[str, Field(description="Plain-text message body.")],
        cc: Annotated[list[str] | None, Field(default=None)] = None,
        bcc: Annotated[list[str] | None, Field(default=None, description="Bcc recipient email addresses.")] = None,
        thread_id: Annotated[
            str | None, Field(default=None, description="Existing Gmail thread ID to draft a reply within.")
        ] = None,
    ) -> Draft:
        """Create a Gmail draft (never sent automatically — the operator sends it from Gmail)."""
        args = CreateGmailDraftArgs(to=to, subject=subject, body=body, cc=cc or [], bcc=bcc or [], thread_id=thread_id)
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


def build_gmail_client_from_token(access_token: str | None) -> GmailToolsClient:
    """Build the Gmail client for one call from the acting Operator's access token."""
    return GmailToolsClient(build_google_api_service("gmail", "v1", access_token))
