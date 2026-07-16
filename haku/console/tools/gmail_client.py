"""Gmail operations behind haku-console's `gmail` MCP server.

The read tools mirror Gmail's REST API: `labels_list`/`labels_get`/`threads_list`/
`threads_get`/`messages_get`/`filters_list`/`filters_get`/`drafts_list`/`drafts_get` return
Gmail's own resource shapes (`gmail_api.messages`, `gmail_api.labels`, `gmail_api.filters`)
**verbatim** — no content-type prioritization, no body decoding, no field flattening. `format`
passes straight through to Gmail. The write tools (draft create/update/delete, thread-label
changes, label CRUD, filter create/delete) act on any resource and are mediated by
haku-console's manual-or-policy approval pipeline. See `haku/docs/security.md`.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from itertools import batched
from typing import Any

from pydantic import BaseModel, Field, model_validator

from gmail_api.filters import FilterAction, FilterCriteria, FiltersListResponse, GmailFilter
from gmail_api.labels import CreateLabelRequest, GmailLabel, LabelsListResponse, LabelType, PatchLabelRequest
from gmail_api.messages import (
    Draft,
    DraftsListResponse,
    Message,
    MessageFormat,
    Thread,
    ThreadFormat,
    ThreadsListResponse,
)

GMAIL_SERVER_ID = "gmail"
# Gmail's batch-request guide recommends capping requests-per-batch at 100. Used for
# `threads.modify` calls.
_MAX_GMAIL_BATCH_SIZE = 100


# --- write-tool models (this surface takes label *names* for convenience and creates
#     missing ones, unlike the read surface which mirrors Gmail's id-based resources) ---
class GmailLabelRef(BaseModel):
    name: str
    id: str


class ModifyGmailThreadLabelsArgs(BaseModel):
    """Add and/or remove Gmail labels across a batch of threads in one call."""

    thread_ids: list[str] = Field(min_length=1, description="Gmail thread IDs to modify in one batch.")
    add: list[str] = Field(default_factory=list, description="Label names to add to every thread; created if new.")
    remove: list[str] = Field(default_factory=list, description="Label names to remove from every thread; must exist.")

    @model_validator(mode="after")
    def _at_least_one_change(self) -> ModifyGmailThreadLabelsArgs:
        if not self.add and not self.remove:
            raise ValueError("must specify at least one label in add or remove")
        if overlap := set(self.add) & set(self.remove):
            raise ValueError(f"label(s) {sorted(overlap)} cannot be both added and removed in the same call")
        return self


class ModifyGmailThreadLabelsResult(BaseModel):
    added: list[GmailLabelRef]
    removed: list[GmailLabelRef]
    thread_count: int


class CreateGmailDraftArgs(BaseModel):
    """Create a Gmail draft (never sent automatically — the operator sends it from Gmail)."""

    to: list[str] = Field(min_length=1, description="Recipient email addresses.")
    subject: str
    body: str = Field(description="Plain-text message body.")
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list, description="Bcc recipient email addresses.")
    thread_id: str | None = Field(default=None, description="Existing Gmail thread ID to draft a reply within.")


class UpdateGmailDraftArgs(CreateGmailDraftArgs):
    """Replace a Gmail draft's message (mirrors users.drafts.update)."""

    draft_id: str = Field(description="ID of the existing draft to replace.")


# --- read-tool input ---
class SearchThreadsArgs(BaseModel):
    query: str = Field(
        description="Gmail search query, same syntax as the Gmail search box "
        "(e.g. 'from:alice after:2026/01/01 is:unread')."
    )
    max_results: int = Field(default=25, ge=1, le=500, description="Maximum threads per page.")
    page_token: str | None = Field(
        default=None, description="`next_page_token` from a previous response; omit for the first page."
    )


class ListDraftsArgs(BaseModel):
    query: str | None = Field(default=None, description="Optional Gmail search query to filter drafts.")
    max_results: int = Field(default=25, ge=1, le=500, description="Maximum drafts per page.")
    page_token: str | None = Field(
        default=None, description="`next_page_token` from a previous response; omit for the first page."
    )


class GmailToolsClient:
    """The `gmail` server's Gmail tool operations over a raw Gmail service."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def _user_labels(self) -> list[GmailLabel]:
        response = self.service.users().labels().list(userId="me").execute()
        labels = [GmailLabel.model_validate(label) for label in response.get("labels", [])]
        return [label for label in labels if label.type == LabelType.USER]

    # --- reads: return Gmail's own resource shapes verbatim ---
    def labels_list(self) -> LabelsListResponse:
        return LabelsListResponse.model_validate(self.service.users().labels().list(userId="me").execute())

    def labels_get(self, label_id: str) -> GmailLabel:
        return GmailLabel.model_validate(self.service.users().labels().get(userId="me", id=label_id).execute())

    def threads_list(self, args: SearchThreadsArgs) -> ThreadsListResponse:
        params: dict[str, Any] = {"userId": "me", "q": args.query, "maxResults": args.max_results}
        if args.page_token is not None:
            params["pageToken"] = args.page_token
        return ThreadsListResponse.model_validate(self.service.users().threads().list(**params).execute())

    def threads_get(self, thread_id: str, thread_format: ThreadFormat) -> Thread:
        return Thread.model_validate(
            self.service.users().threads().get(userId="me", id=thread_id, format=thread_format.value).execute()
        )

    def messages_get(self, message_id: str, message_format: MessageFormat) -> Message:
        return Message.model_validate(
            self.service.users().messages().get(userId="me", id=message_id, format=message_format.value).execute()
        )

    def filters_list(self) -> FiltersListResponse:
        return FiltersListResponse.model_validate(self.service.users().settings().filters().list(userId="me").execute())

    def filters_get(self, filter_id: str) -> GmailFilter:
        return GmailFilter.model_validate(
            self.service.users().settings().filters().get(userId="me", id=filter_id).execute()
        )

    def filters_create(self, criteria: FilterCriteria, action: FilterAction) -> GmailFilter:
        body = {
            "criteria": criteria.model_dump(by_alias=True, exclude_none=True),
            "action": action.model_dump(by_alias=True, exclude_none=True),
        }
        return GmailFilter.model_validate(
            self.service.users().settings().filters().create(userId="me", body=body).execute()
        )

    def filters_delete(self, filter_id: str) -> None:
        self.service.users().settings().filters().delete(userId="me", id=filter_id).execute()

    def drafts_list(self, args: ListDraftsArgs) -> DraftsListResponse:
        params: dict[str, Any] = {"userId": "me", "maxResults": args.max_results}
        if args.query is not None:
            params["q"] = args.query
        if args.page_token is not None:
            params["pageToken"] = args.page_token
        return DraftsListResponse.model_validate(self.service.users().drafts().list(**params).execute())

    def drafts_get(self, draft_id: str, draft_format: MessageFormat) -> Draft:
        return Draft.model_validate(
            self.service.users().drafts().get(userId="me", id=draft_id, format=draft_format.value).execute()
        )

    def drafts_delete(self, draft_id: str) -> None:
        self.service.users().drafts().delete(userId="me", id=draft_id).execute()

    # --- writes ---
    def threads_modify_labels(self, args: ModifyGmailThreadLabelsArgs) -> ModifyGmailThreadLabelsResult:
        existing = {label.name: label.id for label in self._user_labels()}
        if missing := [name for name in args.remove if name not in existing]:
            raise ValueError(f"label(s) {missing} do not exist")

        added = [self._get_or_create(name, existing) for name in args.add]
        removed = [GmailLabelRef(name=name, id=existing[name]) for name in args.remove]
        body: dict[str, list[str]] = {}
        if added:
            body["addLabelIds"] = [label.id for label in added]
        if removed:
            body["removeLabelIds"] = [label.id for label in removed]
        errors: dict[str, Exception] = {}

        def record_error(thread_id: str, _response: object, exception: Exception | None) -> None:
            if exception is not None:
                errors[thread_id] = exception

        for chunk in batched(args.thread_ids, _MAX_GMAIL_BATCH_SIZE, strict=False):
            batch_request = self.service.new_batch_http_request(callback=record_error)
            for thread_id in chunk:
                batch_request.add(
                    self.service.users().threads().modify(userId="me", id=thread_id, body=body), request_id=thread_id
                )
            batch_request.execute()
        if errors:
            raise ExceptionGroup(
                f"failed to modify {len(errors)}/{len(args.thread_ids)} thread(s)", list(errors.values())
            )
        return ModifyGmailThreadLabelsResult(added=added, removed=removed, thread_count=len(args.thread_ids))

    def _get_or_create(self, name: str, existing: dict[str, str]) -> GmailLabelRef:
        if label_id := existing.get(name):
            return GmailLabelRef(name=name, id=label_id)
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        created = GmailLabel.model_validate(self.service.users().labels().create(userId="me", body=body).execute())
        return GmailLabelRef(name=created.name, id=created.id)

    def drafts_create(self, args: CreateGmailDraftArgs) -> Draft:
        return Draft.model_validate(self.service.users().drafts().create(userId="me", body=_draft_body(args)).execute())

    def drafts_update(self, args: UpdateGmailDraftArgs) -> Draft:
        body = {"id": args.draft_id, **_draft_body(args)}
        return Draft.model_validate(
            self.service.users().drafts().update(userId="me", id=args.draft_id, body=body).execute()
        )

    def labels_create(self, request: CreateLabelRequest) -> GmailLabel:
        body = request.model_dump(by_alias=True, exclude_none=True)
        return GmailLabel.model_validate(self.service.users().labels().create(userId="me", body=body).execute())

    def labels_patch(self, label_id: str, request: PatchLabelRequest) -> GmailLabel:
        body = request.model_dump(by_alias=True, exclude_none=True)
        return GmailLabel.model_validate(
            self.service.users().labels().patch(userId="me", id=label_id, body=body).execute()
        )

    def labels_delete(self, label_id: str) -> None:
        self.service.users().labels().delete(userId="me", id=label_id).execute()


def _draft_body(args: CreateGmailDraftArgs) -> dict[str, Any]:
    """Build the `message` body for `drafts.create`/`drafts.update` from plain-text fields."""
    message = MIMEText(args.body)
    message["To"] = ", ".join(args.to)
    message["Subject"] = args.subject
    if args.cc:
        message["Cc"] = ", ".join(args.cc)
    if args.bcc:
        message["Bcc"] = ", ".join(args.bcc)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body: dict[str, Any] = {"message": {"raw": raw}}
    if args.thread_id:
        body["message"]["threadId"] = args.thread_id
    return body
