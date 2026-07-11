"""Tests for GmailToolsClient over a fake googleapiclient-shaped Gmail service.

The service (the network seam) is faked; every label/message/thread it returns is built
from a real `gmail_api` model dumped to the wire shape, so the client's `model_validate`
round-trips genuine resources rather than hand-shaped dicts.
"""

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_bazel
from pydantic import BaseModel

from gmail_api.labels import CreateLabelRequest, GmailLabel, LabelType, PatchLabelRequest
from gmail_api.messages import (
    Draft,
    Message,
    MessageFormat,
    MessagePart,
    MessagePartBody,
    MessagePartHeader,
    Thread,
    ThreadFormat,
    ThreadsListResponse,
)
from haku.console.tools.gmail_client import (
    BatchModifyGmailThreadLabelsArgs,
    CreateGmailDraftArgs,
    GmailLabelRef,
    GmailToolsClient,
    SearchThreadsArgs,
    preview_gmail_threads,
)


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True, exclude_none=True)


class _Executable:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class _ThreadRequest:
    """A pending users.threads.{get,modify}: executes directly (threads_get) or is folded
    into a batch (preview_gmail_threads / modify_threads), which reads `result` back per id."""

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
        # A missing thread resolves to result=None, mirroring an inaccessible thread whose
        # callback carries no response — preview_gmail_threads then skips it.
        for request_id, request in self._requests:
            self._callback(request_id, request.result, None)


class _FakeLabels:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def list(self, *, userId):  # noqa: N803 -- mirrors Gmail's kwarg casing
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

    def list(self, *, userId, **kwargs):  # noqa: N803
        self._svc.calls.append(("threads.list", kwargs))
        return _Executable(self._svc.threads_list_response)

    def get(self, *, userId, id, format, metadataHeaders=None):  # noqa: N803
        self._svc.calls.append(("threads.get", {"id": id, "format": format}))
        return _ThreadRequest(self._svc.thread_responses.get(id))

    def modify(self, *, userId, id, body):  # noqa: N803
        return _ThreadRequest({})


class _FakeMessages:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def get(self, *, userId, id, format):  # noqa: N803
        self._svc.calls.append(("messages.get", {"id": id, "format": format}))
        return _Executable(self._svc.message_responses[id])


class _FakeDrafts:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def create(self, *, userId, body):  # noqa: N803
        self._svc.calls.append(("drafts.create", body))
        return _Executable(self._svc.draft_response)


class _FakeUsers:
    def __init__(self, svc: "_FakeGmailService") -> None:
        self._svc = svc

    def labels(self) -> _FakeLabels:
        return _FakeLabels(self._svc)

    def threads(self) -> _FakeThreads:
        return _FakeThreads(self._svc)

    def messages(self) -> _FakeMessages:
        return _FakeMessages(self._svc)

    def drafts(self) -> _FakeDrafts:
        return _FakeDrafts(self._svc)


@dataclass
class _FakeGmailService:
    labels: list[GmailLabel] = field(default_factory=list)
    label_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    thread_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    message_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    threads_list_response: dict[str, Any] = field(default_factory=dict)
    draft_response: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_label_seq: int = 0

    def users(self) -> _FakeUsers:
        return _FakeUsers(self)

    def new_batch_http_request(self, callback: Any) -> _FakeBatch:
        return _FakeBatch(callback)


@pytest.fixture
def svc() -> _FakeGmailService:
    # Empty by default; each test populates the fields its call reads. GmailToolsClient reads the
    # service lazily (at call time), so a test configures `svc` after the `client` fixture built it.
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


def test_list_labels_returns_real_label_models(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.labels = [
        GmailLabel(id="Label_1", name="haku/triaged", type=LabelType.USER),
        GmailLabel(id="INBOX", name="INBOX", type=LabelType.SYSTEM),
    ]
    result = client.labels_list()
    by_name = {label.name: label for label in result.labels}
    assert by_name["haku/triaged"].id == "Label_1"
    assert by_name["INBOX"].type == LabelType.SYSTEM


def test_get_label_parses_counts(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.label_responses["Label_1"] = _dump(
        GmailLabel(id="Label_1", name="haku/x", type=LabelType.USER, threads_total=3)
    )
    label = client.labels_get("Label_1")
    assert label.name == "haku/x"
    assert label.threads_total == 3
    assert _call(svc, "labels.get") == {"id": "Label_1"}


def test_search_threads_passes_page_token_and_returns_pagination(
    svc: _FakeGmailService, client: GmailToolsClient
) -> None:
    svc.threads_list_response = _dump(
        ThreadsListResponse(threads=[Thread(id="t1", snippet="hi")], next_page_token="NEXT", result_size_estimate=42)
    )
    result = client.threads_list(SearchThreadsArgs(query="is:unread", max_results=10, page_token="PREV"))
    assert [thread.id for thread in result.threads] == ["t1"]
    assert result.next_page_token == "NEXT"
    assert result.result_size_estimate == 42
    assert _call(svc, "threads.list") == {"q": "is:unread", "maxResults": 10, "pageToken": "PREV"}


def test_search_threads_omits_page_token_on_first_page(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.threads_list_response = _dump(ThreadsListResponse())
    client.threads_list(SearchThreadsArgs(query="x"))
    assert "pageToken" not in _call(svc, "threads.list")


def test_get_thread_passes_format_and_parses_messages(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    thread = Thread(id="t1", snippet="hello", messages=[Message(id="m1", thread_id="t1", label_ids=["INBOX"])])
    svc.thread_responses["t1"] = _dump(thread)
    result = client.threads_get("t1", ThreadFormat.METADATA)
    assert result.messages is not None
    assert [message.id for message in result.messages] == ["m1"]
    assert _call(svc, "threads.get") == {"id": "t1", "format": "metadata"}


def test_get_message_full_returns_payload_verbatim(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    message = Message(
        id="m1",
        thread_id="t1",
        label_ids=["INBOX", "Label_1"],
        snippet="hi",
        payload=MessagePart(
            mime_type="multipart/alternative",
            headers=[MessagePartHeader(name="Subject", value="Hello")],
            parts=[MessagePart(mime_type="text/plain", body=MessagePartBody(size=5, data="aGVsbG8="))],
        ),
    )
    svc.message_responses["m1"] = _dump(message)
    result = client.messages_get("m1", MessageFormat.FULL)
    assert result.label_ids == ["INBOX", "Label_1"]
    # Body data is returned exactly as Gmail gives it (base64url), never decoded.
    assert result.payload is not None
    assert result.payload.parts is not None
    part_body = result.payload.parts[0].body
    assert part_body is not None
    assert part_body.data == "aGVsbG8="
    assert _call(svc, "messages.get") == {"id": "m1", "format": "full"}


def test_get_message_raw_passes_raw_format(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.message_responses["m1"] = _dump(Message(id="m1", raw="UkFXLUJZVEVT"))
    result = client.messages_get("m1", MessageFormat.RAW)
    assert result.raw == "UkFXLUJZVEVT"
    assert _call(svc, "messages.get") == {"id": "m1", "format": "raw"}


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
        CreateGmailDraftArgs(to=["a@example.com"], cc=["b@example.com"], subject="Hi", body="Hello there")
    )
    assert result.id == "d1"
    assert result.message is not None
    assert result.message.id == "m1"
    body = _call(svc, "drafts.create")
    decoded = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
    assert "To: a@example.com" in decoded
    assert "Cc: b@example.com" in decoded
    assert "Subject: Hi" in decoded
    assert "Hello there" in decoded


def test_create_draft_reply_carries_thread_id(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.draft_response = _dump(_draft("d1", "m1"))
    client.drafts_create(CreateGmailDraftArgs(to=["a@example.com"], subject="Re", body="ok", thread_id="t1"))
    assert _call(svc, "drafts.create")["message"]["threadId"] == "t1"


def test_batch_modify_creates_missing_add_label_and_modifies_threads(
    svc: _FakeGmailService, client: GmailToolsClient
) -> None:
    result = client.threads_batch_modify(BatchModifyGmailThreadLabelsArgs(thread_ids=["t1", "t2"], add=["urgent"]))
    assert result.thread_count == 2
    assert [label.name for label in result.added] == ["urgent"]
    assert any(called == "labels.create" for called, _ in svc.calls)


def test_batch_modify_reuses_existing_label(svc: _FakeGmailService, client: GmailToolsClient) -> None:
    svc.labels = [GmailLabel(id="Label_9", name="urgent", type=LabelType.USER)]
    result = client.threads_batch_modify(BatchModifyGmailThreadLabelsArgs(thread_ids=["t1"], add=["urgent"]))
    assert result.added == [GmailLabelRef(name="urgent", id="Label_9")]
    assert not any(called == "labels.create" for called, _ in svc.calls)  # not re-created


def test_batch_modify_remove_requires_existing_label(client: GmailToolsClient) -> None:
    with pytest.raises(ValueError, match="do not exist"):
        client.threads_batch_modify(BatchModifyGmailThreadLabelsArgs(thread_ids=["t1"], remove=["ghost"]))


def test_preview_threads_extracts_subject_snippet_and_labels(svc: _FakeGmailService) -> None:
    svc.labels = [GmailLabel(id="Label_1", name="haku/triaged", type=LabelType.USER)]
    svc.thread_responses["t1"] = _dump(
        Thread(
            id="t1",
            messages=[
                Message(
                    id="m1",
                    snippet="hello world",
                    label_ids=["Label_1", "UNKNOWN_ID"],
                    payload=MessagePart(headers=[MessagePartHeader(name="Subject", value="Test subject")]),
                )
            ],
        )
    )
    previews = preview_gmail_threads(svc, ["t1", "missing"])
    assert previews.keys() == {"t1"}  # "missing" resolves to None -> omitted
    assert previews["t1"].subject == "Test subject"
    assert previews["t1"].snippet == "hello world"
    assert previews["t1"].current_label_names == ["haku/triaged"]  # UNKNOWN_ID dropped
    assert previews["t1"].gmail_url == "https://mail.google.com/mail/u/0/#all/t1"


if __name__ == "__main__":
    pytest_bazel.main()
