"""Tests for GmailToolsClient and preview_gmail_threads over a fake googleapiclient-shaped
Gmail service."""

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_bazel

from haku.console.tools.google_gmail import (
    BatchModifyGmailThreadLabelsArgs,
    CreateGmailDraftArgs,
    GmailLabelRef,
    GmailToolsClient,
    preview_gmail_threads,
)


@dataclass
class _FakeRequest:
    kind: str
    payload: dict[str, Any]


class _FakeBatchRequest:
    def __init__(self, callback: Any, responses: dict[str, dict[str, Any]]) -> None:
        self._callback = callback
        self._responses = responses
        self.requests: list[tuple[str, _FakeRequest]] = []

    def add(self, request: _FakeRequest, *, request_id: str) -> None:
        self.requests.append((request_id, request))

    def execute(self) -> None:
        for request_id, request in self.requests:
            if request.kind == "threads.get":
                self._callback(request_id, self._responses.get(request_id), None)
            else:
                self._callback(request_id, {}, None)


class _FakeLabels:
    def __init__(self, service: "_FakeGmailService") -> None:
        self._service = service

    def list(self, *, userId):  # noqa: N803 -- mirrors Gmail's kwarg casing
        return _FakeRequest(
            "labels.list", {"labels": [label.model_dump(by_alias=True) for label in self._service.labels]}
        )

    def create(self, *, userId, body):  # noqa: N803
        return _FakeRequest("labels.create", body)


class _FakeThreads:
    def __init__(self, service: "_FakeGmailService") -> None:
        self._service = service

    def modify(self, *, userId, id, body):  # noqa: N803
        return _FakeRequest("threads.modify", {"id": id, **body})

    def get(self, *, userId, id, format):  # noqa: N803
        return _FakeRequest("threads.get", {"id": id})


class _FakeDrafts:
    def __init__(self, service: "_FakeGmailService") -> None:
        self._service = service

    def create(self, *, userId, body):  # noqa: N803
        self._service.created_drafts.append(body)
        return _FakeExecutable({"id": "draft1", "message": {"id": "msg1"}})


class _FakeExecutable:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self) -> dict[str, Any]:
        return self._result


class _FakeUsers:
    def __init__(self, service: "_FakeGmailService") -> None:
        self._service = service

    def labels(self) -> _FakeLabels:
        return _FakeLabels(self._service)

    def threads(self) -> _FakeThreads:
        return _FakeThreads(self._service)

    def drafts(self) -> _FakeDrafts:
        return _FakeDrafts(self._service)


@dataclass
class _FakeLabel:
    id: str
    name: str
    type: str = "user"

    def model_dump(self, *, by_alias: bool) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type}


@dataclass
class _FakeGmailService:
    labels: list[_FakeLabel] = field(default_factory=list)
    thread_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_drafts: list[dict[str, Any]] = field(default_factory=list)
    _next_label_id: int = 1

    def users(self) -> _FakeUsers:
        return _FakeUsers(self)

    def new_batch_http_request(self, callback: Any) -> _FakeBatchRequest:
        return _FakeBatchRequest(callback, self.thread_responses)

    # Executed by GmailLabelBackend.create_label directly (not batched).
    def _create_label_execute(self, body: dict[str, Any]) -> dict[str, Any]:
        label_id = f"Label_{self._next_label_id}"
        self._next_label_id += 1
        self.labels.append(_FakeLabel(id=label_id, name=body["name"]))
        return {"id": label_id, "name": body["name"], "type": "user"}


def _patch_execute(monkeypatch: pytest.MonkeyPatch, service: _FakeGmailService) -> None:
    def execute(self: _FakeRequest) -> dict[str, Any]:
        if self.kind == "labels.list":
            return self.payload
        if self.kind == "labels.create":
            return service._create_label_execute(self.payload)
        if self.kind == "threads.modify":
            return {}
        raise AssertionError(f"unexpected direct execute() on {self.kind}")

    monkeypatch.setattr(_FakeRequest, "execute", execute, raising=False)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> _FakeGmailService:
    svc = _FakeGmailService()
    _patch_execute(monkeypatch, svc)
    return svc


def test_batch_modify_creates_missing_add_label_and_modifies_threads(service: _FakeGmailService) -> None:
    client = GmailToolsClient(service)
    result = client.batch_modify_thread_labels(
        BatchModifyGmailThreadLabelsArgs(thread_ids=["t1", "t2"], add=["urgent"])
    )
    assert result.thread_count == 2
    assert [label.name for label in result.added] == ["urgent"]
    assert any(label.name == "urgent" for label in service.labels)


def test_batch_modify_reuses_existing_label(service: _FakeGmailService) -> None:
    service.labels.append(_FakeLabel(id="Label_9", name="urgent"))
    client = GmailToolsClient(service)
    result = client.batch_modify_thread_labels(BatchModifyGmailThreadLabelsArgs(thread_ids=["t1"], add=["urgent"]))
    assert result.added == [GmailLabelRef(name="urgent", id="Label_9")]
    assert len(service.labels) == 1  # not re-created


def test_batch_modify_remove_requires_existing_label(service: _FakeGmailService) -> None:
    client = GmailToolsClient(service)
    with pytest.raises(ValueError, match="do not exist"):
        client.batch_modify_thread_labels(BatchModifyGmailThreadLabelsArgs(thread_ids=["t1"], remove=["ghost"]))


def test_create_draft_encodes_mime_and_returns_ids(service: _FakeGmailService) -> None:
    client = GmailToolsClient(service)
    result = client.create_draft(
        CreateGmailDraftArgs(to=["a@example.com"], cc=["b@example.com"], subject="Hi", body="Hello there")
    )
    assert result.draft_id == "draft1"
    assert result.message_id == "msg1"
    raw = service.created_drafts[0]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "To: a@example.com" in decoded
    assert "Cc: b@example.com" in decoded
    assert "Subject: Hi" in decoded
    assert "Hello there" in decoded


def test_create_draft_reply_carries_thread_id(service: _FakeGmailService) -> None:
    client = GmailToolsClient(service)
    client.create_draft(CreateGmailDraftArgs(to=["a@example.com"], subject="Re: Hi", body="ok", thread_id="t1"))
    assert service.created_drafts[0]["message"]["threadId"] == "t1"


def test_preview_threads_extracts_subject_snippet_and_labels(service: _FakeGmailService) -> None:
    service.labels.append(_FakeLabel(id="Label_1", name="haku/triaged"))
    service.thread_responses["t1"] = {
        "messages": [
            {
                "snippet": "hello world",
                "labelIds": ["Label_1", "UNKNOWN_ID"],
                "payload": {"headers": [{"name": "Subject", "value": "Test subject"}]},
            }
        ]
    }
    previews = preview_gmail_threads(service, ["t1", "missing"])
    assert previews.keys() == {"t1"}  # "missing" has no response -> omitted
    preview = previews["t1"]
    assert preview.subject == "Test subject"
    assert preview.snippet == "hello world"
    assert preview.current_label_names == ["haku/triaged"]
    assert preview.gmail_url == "https://mail.google.com/mail/u/0/#all/t1"


def test_preview_threads_subject_is_none_not_a_placeholder_string(service: _FakeGmailService) -> None:
    # Whether/how a missing Subject renders (e.g. "(no subject)") is a frontend
    # decision; the backend passes through the raw absence.
    service.thread_responses["t1"] = {"messages": [{"snippet": "", "labelIds": [], "payload": {"headers": []}}]}
    assert preview_gmail_threads(service, ["t1"])["t1"].subject is None


if __name__ == "__main__":
    pytest_bazel.main()
