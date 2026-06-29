"""Low-level Gmail label operations behind a typed seam.

`LabelBackend` is the I/O boundary `LabelClient` depends on; `GmailLabelBackend`
implements it over the Gmail REST API, and tests substitute an in-memory fake.
"""

from typing import Protocol

from googleapiclient.discovery import Resource

from gmail_api.labels import GmailLabel


class LabelBackend(Protocol):
    """The Gmail label operations `LabelClient` needs. No namespace enforcement here."""

    def list_labels(self) -> list[GmailLabel]: ...
    def create_label(self, name: str) -> GmailLabel: ...
    def rename_label(self, label_id: str, new_name: str) -> GmailLabel: ...
    def delete_label(self, label_id: str) -> None: ...
    def modify_thread(self, thread_id: str, *, add: list[str], remove: list[str]) -> None: ...


class GmailLabelBackend:
    """`LabelBackend` over the Gmail REST API (`users.labels` + `users.threads.modify`)."""

    def __init__(self, service: Resource) -> None:
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

    def modify_thread(self, thread_id: str, *, add: list[str], remove: list[str]) -> None:
        body: dict[str, list[str]] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        self._service.users().threads().modify(userId="me", id=thread_id, body=body).execute()
