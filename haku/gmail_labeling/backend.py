"""Low-level Gmail label operations behind a typed seam.

`LabelBackend` is the I/O boundary `LabelClient` depends on; `GmailLabelBackend`
implements it over the Gmail REST API, and tests substitute an in-memory fake.
"""

from collections.abc import Sequence
from itertools import batched
from typing import Any, Protocol

from gmail_api.labels import GmailLabel

# Gmail has no `threads.batchModify` (only `messages.batchModify`, a different resource).
# Batching thread modifications means folding many `threads.modify` calls into few HTTP
# requests via `new_batch_http_request` -- the mechanism Gmail's own batch guide describes
# (https://developers.google.com/gmail/api/guides/batch) -- capped at its recommended 100
# calls per batch.
_MAX_BATCH_SIZE = 100


class LabelBackend(Protocol):
    """The Gmail label operations `LabelClient` needs. No namespace enforcement here."""

    def list_labels(self) -> list[GmailLabel]: ...
    def create_label(self, name: str) -> GmailLabel: ...
    def rename_label(self, label_id: str, new_name: str) -> GmailLabel: ...
    def delete_label(self, label_id: str) -> None: ...
    def modify_threads(self, thread_ids: Sequence[str], *, add: list[str], remove: list[str]) -> None: ...


class GmailLabelBackend:
    """`LabelBackend` over the Gmail REST API (`users.labels` + `users.threads.modify`).

    `service` is google-api-python-client's dynamically-generated, untyped Resource.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def list_labels(self) -> list[GmailLabel]:
        result = self._service.users().labels().list(userId="me").execute()
        return [GmailLabel.model_validate(label) for label in result.get("labels", [])]

    def create_label(self, name: str) -> GmailLabel:
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        return GmailLabel.model_validate(self._service.users().labels().create(userId="me", body=body).execute())

    def rename_label(self, label_id: str, new_name: str) -> GmailLabel:
        return GmailLabel.model_validate(
            self._service.users().labels().patch(userId="me", id=label_id, body={"name": new_name}).execute()
        )

    def delete_label(self, label_id: str) -> None:
        self._service.users().labels().delete(userId="me", id=label_id).execute()

    def modify_threads(self, thread_ids: Sequence[str], *, add: list[str], remove: list[str]) -> None:
        body: dict[str, list[str]] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove

        errors: dict[str, Exception] = {}

        def record_error(thread_id: str, _response: object, exception: Exception | None) -> None:
            if exception is not None:
                errors[thread_id] = exception

        for chunk in batched(thread_ids, _MAX_BATCH_SIZE, strict=False):
            batch_request = self._service.new_batch_http_request(callback=record_error)
            for thread_id in chunk:
                batch_request.add(
                    self._service.users().threads().modify(userId="me", id=thread_id, body=body), request_id=thread_id
                )
            batch_request.execute()

        if errors:
            raise ExceptionGroup(f"failed to modify {len(errors)}/{len(thread_ids)} thread(s)", list(errors.values()))
