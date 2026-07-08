"""Tests for GoogleToolProvider (tool dispatch/validation/metadata) and the
thread-previews router endpoint."""

from typing import Any
from unittest.mock import Mock

import pytest
import pytest_bazel

from haku.console.google_tools import GOOGLE_SERVER_ID, GoogleToolProvider
from haku.console.google_tools_models import (
    BatchModifyGmailThreadLabelsResult,
    CreateCalendarEventResult,
    CreateGmailDraftResult,
    GmailLabelRef,
    GmailThreadPreview,
)


def _provider(gmail: Any = None, calendar: Any = None) -> GoogleToolProvider:
    return GoogleToolProvider(gmail or Mock(), calendar or Mock())


async def test_execute_create_calendar_event_dispatches_to_calendar_client() -> None:
    calendar = Mock()
    calendar.create_event.return_value = CreateCalendarEventResult(event_id="evt1", html_link="https://x/evt1")
    provider = _provider(calendar=calendar)
    result = await provider.execute(
        "create_calendar_event", {"summary": "S", "start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}
    )
    assert result == {"event_id": "evt1", "html_link": "https://x/evt1"}
    calendar.create_event.assert_called_once()


async def test_execute_batch_modify_gmail_thread_labels_dispatches_to_gmail_client() -> None:
    gmail = Mock()
    gmail.batch_modify_thread_labels.return_value = BatchModifyGmailThreadLabelsResult(
        added=[GmailLabelRef(name="urgent", id="Label_1")], removed=[], thread_count=1
    )
    provider = _provider(gmail=gmail)
    result = await provider.execute("batch_modify_gmail_thread_labels", {"thread_ids": ["t1"], "add": ["urgent"]})
    assert result["thread_count"] == 1
    gmail.batch_modify_thread_labels.assert_called_once()


async def test_execute_create_gmail_draft_dispatches_to_gmail_client() -> None:
    gmail = Mock()
    gmail.create_draft.return_value = CreateGmailDraftResult(draft_id="d1", message_id="m1")
    provider = _provider(gmail=gmail)
    result = await provider.execute("create_gmail_draft", {"to": ["a@example.com"], "subject": "S", "body": "B"})
    assert result == {"draft_id": "d1", "message_id": "m1"}


async def test_execute_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        await _provider().execute("delete_everything", {})


async def test_execute_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="invalid arguments"):
        await _provider().execute("create_gmail_draft", {"to": [], "subject": "S", "body": "B"})  # to: min_length=1


async def test_metadata_lists_all_three_tools_with_schemas() -> None:
    metadata = await _provider().metadata()
    assert metadata.server_id == GOOGLE_SERVER_ID
    names = {tool.name for tool in metadata.tools}
    assert names == {"create_calendar_event", "batch_modify_gmail_thread_labels", "create_gmail_draft"}
    for tool in metadata.tools:
        assert tool.input_schema  # non-empty JSON schema


def test_preview_threads_wraps_gmail_client(monkeypatch: pytest.MonkeyPatch) -> None:
    gmail = Mock()
    gmail.preview_threads.return_value = {
        "t1": GmailThreadPreview(subject="S", snippet="s", current_label_names=[], gmail_url="https://x/t1")
    }
    provider = _provider(gmail=gmail)
    response = provider.preview_threads(["t1"])
    assert response.threads["t1"].subject == "S"
    gmail.preview_threads.assert_called_once_with(["t1"])


def test_gmail_thread_previews_endpoint_503s_when_unconfigured(make_client) -> None:
    with make_client() as client:
        resp = client.get("/api/google/gmail/thread-previews", params={"thread_id": ["t1"]})
        assert resp.status_code == 503


def test_gmail_thread_previews_endpoint_returns_preview(make_client) -> None:
    gmail = Mock()
    gmail.preview_threads.return_value = {
        "t1": GmailThreadPreview(subject="Test", snippet="hi", current_label_names=["haku/x"], gmail_url="https://x/t1")
    }
    provider = _provider(gmail=gmail)
    with make_client(google_tool_provider=provider) as client:
        resp = client.get("/api/google/gmail/thread-previews", params={"thread_id": ["t1"]})
        assert resp.status_code == 200
        assert resp.json()["threads"]["t1"]["subject"] == "Test"


if __name__ == "__main__":
    pytest_bazel.main()
