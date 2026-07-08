"""Gmail operations behind haku-console's Google tool provider.

Unlike `haku.gmail_labeling` (autonomous, closed over a label-name namespace by
construction), these tools have **no server-side namespace restriction** — the safety
gate is the human operator approving each call in haku-console, not a structural
invariant here. See `haku/docs/security.md` for the enforcement-inventory entry.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from itertools import batched
from typing import Any

from pydantic import BaseModel, Field, model_validator

from gmail_api.labels import GmailLabel, LabelType
from haku.gmail_labeling.backend import GmailLabelBackend

_THREAD_URL = "https://mail.google.com/mail/u/0/#all/{thread_id}"
# Gmail's batch-request guide recommends capping requests-per-batch at 100.
_MAX_PREVIEW_BATCH_SIZE = 100


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
    """Batch label modify + draft creation + thread preview, over a raw Gmail service."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._backend = GmailLabelBackend(service)

    def _user_labels(self) -> list[GmailLabel]:
        return [label for label in self._backend.list_labels() if label.type == LabelType.USER]

    def batch_modify_thread_labels(self, args: BatchModifyGmailThreadLabelsArgs) -> BatchModifyGmailThreadLabelsResult:
        existing = {label.name: label.id for label in self._user_labels()}
        if missing := [name for name in args.remove if name not in existing]:
            raise ValueError(f"label(s) {missing} do not exist")

        added = [self._get_or_create(name, existing) for name in args.add]
        removed = [GmailLabelRef(name=name, id=existing[name]) for name in args.remove]
        self._backend.modify_threads(
            args.thread_ids, add=[label.id for label in added], remove=[label.id for label in removed]
        )
        return BatchModifyGmailThreadLabelsResult(added=added, removed=removed, thread_count=len(args.thread_ids))

    def _get_or_create(self, name: str, existing: dict[str, str]) -> GmailLabelRef:
        if label_id := existing.get(name):
            return GmailLabelRef(name=name, id=label_id)
        created = self._backend.create_label(name)
        return GmailLabelRef(name=created.name, id=created.id)

    def create_draft(self, args: CreateGmailDraftArgs) -> CreateGmailDraftResult:
        message = MIMEText(args.body)
        message["To"] = ", ".join(args.to)
        message["Subject"] = args.subject
        if args.cc:
            message["Cc"] = ", ".join(args.cc)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body: dict[str, Any] = {"message": {"raw": raw}}
        if args.thread_id:
            body["message"]["threadId"] = args.thread_id
        draft = self._service.users().drafts().create(userId="me", body=body).execute()
        return CreateGmailDraftResult(draft_id=draft["id"], message_id=draft["message"]["id"])

    def preview_threads(self, thread_ids: list[str]) -> dict[str, GmailThreadPreview]:
        id_by_name = {label.id: label.name for label in self._backend.list_labels()}
        previews: dict[str, GmailThreadPreview] = {}

        def record(thread_id: str, response: dict[str, Any] | None, exception: Exception | None) -> None:
            if exception is not None or response is None:
                return
            previews[thread_id] = _preview_from_thread(thread_id, response, id_by_name)

        for chunk in batched(thread_ids, _MAX_PREVIEW_BATCH_SIZE, strict=False):
            batch_request = self._service.new_batch_http_request(callback=record)
            for thread_id in chunk:
                batch_request.add(
                    self._service.users().threads().get(userId="me", id=thread_id, format="metadata"),
                    request_id=thread_id,
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
