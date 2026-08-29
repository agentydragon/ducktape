"""Tests for GmailToolsClient over a fake googleapiclient-shaped Gmail service.

The client now holds only the hand-written **write** surface (draft/label/filter mutations,
batch thread-label changes) plus the `labels_get` id->name helper the approval carve-out uses;
the read tools moved to `google_discovery.py`. The service (the network seam) is faked; every
resource it returns is a real `gmail_api` model dumped to the wire shape, so the client's
`model_validate` round-trips genuine resources rather than hand-shaped dicts.
"""

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_bazel
from pydantic import BaseModel

from gmail_api.filters import FilterAction, FilterCriteria
from gmail_api.labels import CreateLabelRequest, GmailLabel, LabelType, PatchLabelRequest
from gmail_api.messages import Draft, Message
from haku.console.tools.gmail_client import (
    CreateGmailDraftArgs,
    GmailLabelRef,
    GmailToolsClient,
    ModifyGmailThreadLabelsArgs,
    UpdateGmailDraftArgs,
)


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True, exclude_none=True)


class _Executable:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class _ThreadRequest:
    """A pending users.threads.modify request (batched)."""

    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


class _FakeBatch:
    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self._requests: list[tuple[str, _ThreadRequest]] = []

    def add(self, request: _ThreadRequest, *, request_id: str) -> None:
        self._requests.append((request_id, request))

    def execute(self) -> None:
        for request_id, request in self._requests:
            self._callback(request_id, request.result, None)


class _FakeLabels:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def list(self, *, userId):  # noqa: N803 -- mirrors Gmail's kwarg casing; used by _user_labels()
        return _Executable({"labels": [_dump(label) for label in self._svc.labels]})

    def get(self, *, userId, id):  # noqa: N803
        self._svc.calls.append(("labels.get", {"id": id}))
        return _Executable(self._svc.label_responses[id])

    def create(self, *, userId, body):  # noqa: N803
        self._svc.calls.append(("labels.create", body))
        self._svc.created_label_seq += 1
        return _Executable({"id": f"Label_{self._svc.created_label_seq}", "type": "user", **body})

    def patch(self, *, userId, id, body):  # noqa: N803
        self._svc.calls.append(("labels.patch", {"id": id, **body}))
        return _Executable({"id": id, "type": "user", **body})

    def delete(self, *, userId, id):  # noqa: N803
        self._svc.calls.append(("labels.delete", {"id": id}))
        return _Executable({})


class _FakeThreads:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def modify(self, *, userId, id, body):  # noqa: N803
        return _ThreadRequest({})


class _FakeDrafts:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def create(self, *, userId, body):  # noqa: N803
        self._svc.calls.append(("drafts.create", body))
        return _Executable(self._svc.draft_response)

    def update(self, *, userId, id, body):  # noqa: N803
        self._svc.calls.append(("drafts.update", {"id": id, **body}))
        return _Executable(self._svc.draft_response)

    def delete(self, *, userId, id):  # noqa: N803
        self._svc.calls.append(("drafts.delete", {"id": id}))
        return _Executable({})


class _FakeFilters:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def create(self, *, userId, body):  # noqa: N803
        self._svc.created_filter_seq += 1
        result = {"id": f"Filter_{self._svc.created_filter_seq}", **body}
        self._svc.calls.append(("filters.create", body))
        return _Executable(result)

    def delete(self, *, userId, id):  # noqa: N803
        self._svc.calls.append(("filters.delete", {"id": id}))
        return _Executable({})


class _FakeSettings:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def filters(self) -> _FakeFilters:
        return _FakeFilters(self._svc)


class _FakeUsers:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def labels(self) -> _FakeLabels:
        return _FakeLabels(self._svc)

    def threads(self) -> _FakeThreads:
        return _FakeThreads(self._svc)

    def drafts(self) -> _FakeDrafts:
        return _FakeDrafts(self._svc)

    def settings(self) -> _FakeSettings:
        return _FakeSettings(self._svc)


@dataclass
class _FakeGmailService:
    labels: list[GmailLabel] = field(default_factory=list)
    label_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    draft_response: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_label_seq: int = 0
    created_filter_seq: int = 0

    def users(self) -> _FakeUsers:
        return _FakeUsers(self)

    def new_batch_http_request(self, callback: Any) -> _FakeBatch:
        return _FakeBatch(callback)


@pytest.fixture
def svc() -> _FakeGmailService:
    return _FakeGmailService()


@pytest.fixture
def client(svc: _FakeGmailService) -> GmailToolsClient:
    return GmailToolsClient(svc)


def _call(svc: _FakeGmailService, name: str) -> dict[str, Any]:
    matches = [params for called, params in svc.calls if called == name]
    assert len(matches) == 1, f"expected exactly one {name} call, got {matches}"
    return matches[0]


def _draft(draft_id: str, message_id: str) -> Draft:
    return Draft(id=draft_id, message=Message(id=message_id, thread_id="t1"))


def test_get_label_parses_counts(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.label_responses["Label_1"] = _dump(
        GmailLabel(id="Label_1", name="haku/x", type=LabelType.USER, threads_total=3)
    )
    label = client.labels_get("Label_1")
    assert label.name == "haku/x"
    assert label.threads_total == 3
    assert _call(svc, "labels.get") == {"id": "Label_1"}


def test_create_label_sends_body_and_parses(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    label = client.labels_create(CreateLabelRequest(name="receipts/amazon"))
    assert label.name == "receipts/amazon"
    assert label.id == "Label_1"
    body = _call(svc, "labels.create")
    assert body["name"] == "receipts/amazon"
    assert body["labelListVisibility"] == "labelShow"  # camelCase alias, default visibility


def test_patch_label_sends_only_set_fields(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    client.labels_patch("Label_1", PatchLabelRequest(name="renamed"))
    params = _call(svc, "labels.patch")
    assert params == {"id": "Label_1", "name": "renamed"}  # unset visibilities omitted (exclude_none)


def test_delete_label_calls_delete(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    client.labels_delete("Label_9")
    assert _call(svc, "labels.delete") == {"id": "Label_9"}


def test_create_draft_encodes_mime_and_returns_draft_resource(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.draft_response = _dump(_draft("d1", "m1"))
    result = client.drafts_create(
        CreateGmailDraftArgs(
            to=["a@example.com"], cc=["b@example.com"], bcc=["c@example.com"], subject="Hi", body="Hello there"
        )
    )
    assert result.id == "d1"
    assert result.message is not None
    assert result.message.id == "m1"
    body = _call(svc, "drafts.create")
    decoded = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
    assert "To: a@example.com" in decoded
    assert "Cc: b@example.com" in decoded
    assert "Bcc: c@example.com" in decoded
    assert "Subject: Hi" in decoded
    assert "Hello there" in decoded


def test_create_draft_reply_carries_thread_id(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.draft_response = _dump(_draft("d1", "m1"))
    client.drafts_create(CreateGmailDraftArgs(to=["a@example.com"], subject="Re", body="ok", thread_id="t1"))
    assert _call(svc, "drafts.create")["message"]["threadId"] == "t1"


def test_update_draft_replaces_message_and_carries_id(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.draft_response = _dump(_draft("d1", "m1"))
    result = client.drafts_update(
        UpdateGmailDraftArgs(draft_id="d9", to=["a@example.com"], cc=["b@x"], bcc=["c@x"], subject="Re", body="edited")
    )
    assert result.id == "d1"
    params = _call(svc, "drafts.update")
    assert params["id"] == "d9"
    decoded = base64.urlsafe_b64decode(params["message"]["raw"]).decode()
    assert "To: a@example.com" in decoded
    assert "Cc: b@x" in decoded
    assert "Bcc: c@x" in decoded
    assert "Subject: Re" in decoded
    assert "edited" in decoded


def test_delete_draft_calls_delete(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    client.drafts_delete("d9")
    assert _call(svc, "drafts.delete") == {"id": "d9"}


def test_create_filter_sends_criteria_and_action_body(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    result = client.filters_create(FilterCriteria(from_="spam@example.com"), FilterAction(remove_label_ids=["INBOX"]))
    assert result.id == "Filter_1"
    body = _call(svc, "filters.create")
    assert body["criteria"] == {"from": "spam@example.com"}  # camelCase wire alias, exclude_none
    assert body["action"] == {"removeLabelIds": ["INBOX"]}


def test_delete_filter_calls_delete(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    client.filters_delete("F9")
    assert _call(svc, "filters.delete") == {"id": "F9"}


def test_batch_modify_creates_missing_add_label_and_modifies_threads(
    svc: _FakeGmailService, client: GmailToolsClient
) -> None:
    result = client.threads_modify_labels(ModifyGmailThreadLabelsArgs(thread_ids=["t1", "t2"], add=["urgent"]))
    assert result.thread_count == 2
    assert [label.name for label in result.added] == ["urgent"]
    assert any(called == "labels.create" for called, _ in svc.calls)


def test_batch_modify_reuses_existing_label(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.labels = [GmailLabel(id="Label_9", name="urgent", type=LabelType.USER)]
    result = client.threads_modify_labels(ModifyGmailThreadLabelsArgs(thread_ids=["t1"], add=["urgent"]))
    assert result.added == [GmailLabelRef(name="urgent", id="Label_9")]
    assert not any(called == "labels.create" for called, _ in svc.calls)  # not re-created


def test_batch_modify_remove_requires_existing_label(client: GmailToolsClient) -> None:
    with pytest.raises(ValueError, match="do not exist"):
        client.threads_modify_labels(ModifyGmailThreadLabelsArgs(thread_ids=["t1"], remove=["ghost"]))


if __name__ == "__main__":
    pytest_bazel.main()
