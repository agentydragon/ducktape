"""Gmail operations behind haku-console's `gmail` MCP server.

The read tools mirror Gmail's REST API: `labels_list`/`labels_get`/`threads_list`/
`threads_get`/`messages_get` return Gmail's own resource shapes (`gmail_api.messages`,
`gmail_api.labels`) **verbatim** — no content-type prioritization, no body decoding, no
field flattening. `format` passes straight through to Gmail. The write tools (draft
creation, thread-label changes, label CRUD) act on any label/thread and are mediated by
haku-console's manual-or-policy approval pipeline. See `haku/docs/security.md`.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from itertools import batched
from typing import Any

from pydantic import BaseModel, Field, model_validator

from gmail_api.labels import CreateLabelRequest, GmailLabel, LabelsListResponse, LabelType, PatchLabelRequest
from gmail_api.messages import Draft, Message, MessageFormat, Thread, ThreadFormat, ThreadsListResponse

GMAIL_SERVER_ID = "gmail"
_THREAD_URL = "https://mail.google.com/mail/u/0/#all/{thread_id}"
# Gmail's batch-request guide recommends capping requests-per-batch at 100. Used for
# both `threads.modify` calls and approval-preview `threads.get` metadata lookups.
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
    thread_id: str | None = Field(default=None, description="Existing Gmail thread ID to draft a reply within.")


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


# --- approval-preview models (rendered by the console for a pending
#     `threads_modify_labels` call, which itself only carries thread IDs) ---
class GmailThreadPreview(BaseModel):
    # Gmail permits a threadless/no-Subject message; whether that renders as e.g. "(no
    # subject)" is a display decision, so this stays the raw (possibly absent) header.
    subject: str | None
    snippet: str
    current_label_names: list[str]
    gmail_url: str = Field(description="Link to the thread in the Gmail web UI.")


class GmailThreadPreviewsResponse(BaseModel):
    threads: dict[str, GmailThreadPreview] = Field(
        description="Keyed by thread_id; a requested id absent from the map was inaccessible (deleted, wrong account, …)."
    )


class GmailToolsClient:
    """The `gmail` server's Gmail tool operations — reads mirroring the REST API plus
    draft/label writes — over a raw Gmail service. Thread previews are a rendering-support
    read, not a tool op, so they live in the module-level `preview_gmail_threads`, composed
    from the same lower-level service."""

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
        message = MIMEText(args.body)
        message["To"] = ", ".join(args.to)
        message["Subject"] = args.subject
        if args.cc:
            message["Cc"] = ", ".join(args.cc)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body: dict[str, Any] = {"message": {"raw": raw}}
        if args.thread_id:
            body["message"]["threadId"] = args.thread_id
        return Draft.model_validate(self.service.users().drafts().create(userId="me", body=body).execute())

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


def preview_gmail_threads(service: Any, thread_ids: list[str]) -> dict[str, GmailThreadPreview]:
    """Subject/snippet/current-labels for a batch of threads, for rendering a pending or past
    `threads_modify_labels` approval. Composed from lower-level Gmail reads — a batched
    `threads().get(format=metadata)` plus a label id→name lookup — not a tool the agent invokes,
    so it's a free function over the raw service rather than a `GmailToolsClient` method. A
    thread absent from the returned map was inaccessible (deleted, wrong account, …)."""
    labels_response = service.users().labels().list(userId="me").execute()
    labels = [GmailLabel.model_validate(label) for label in labels_response.get("labels", [])]
    id_by_name = {label.id: label.name for label in labels}
    previews: dict[str, GmailThreadPreview] = {}

    def record(thread_id: str, response: dict[str, Any] | None, exception: Exception | None) -> None:
        if exception is not None or response is None:
            return
        previews[thread_id] = _preview_from_thread(thread_id, response, id_by_name)

    for chunk in batched(thread_ids, _MAX_GMAIL_BATCH_SIZE, strict=False):
        batch_request = service.new_batch_http_request(callback=record)
        for thread_id in chunk:
            batch_request.add(
                service.users().threads().get(userId="me", id=thread_id, format="metadata"), request_id=thread_id
            )
        batch_request.execute()
    return previews


def _preview_from_thread(
    thread_id: str, thread: dict[str, Any], label_names_by_id: dict[str, str]
) -> GmailThreadPreview:
    first_message = thread.get("messages", [{}])[0]
    headers = {h["name"]: h["value"] for h in first_message.get("payload", {}).get("headers", [])}
    label_ids = first_message.get("labelIds", [])
    return GmailThreadPreview(
        subject=headers.get("Subject"),
        snippet=first_message.get("snippet", ""),
        current_label_names=sorted(
            label_names_by_id[label_id] for label_id in label_ids if label_id in label_names_by_id
        ),
        gmail_url=_THREAD_URL.format(thread_id=thread_id),
    )
