"""Tests for the `google` in-process MCP server (build_mcp) and the thread-previews
router endpoint."""

from unittest.mock import Mock, patch

import pytest_bazel
from fastmcp import Client

from haku.console.tools.google import GOOGLE_SERVER_ID, build_mcp
from haku.console.tools.google_calendar import CreateCalendarEventResult
from haku.console.tools.google_gmail import (
    BatchModifyGmailThreadLabelsResult,
    CreateGmailDraftResult,
    GmailThreadPreview,
)


def _mcp(gmail=None, calendar=None):
    return build_mcp(gmail or Mock(), calendar or Mock())


async def test_tool_surface():
    async with Client(_mcp()) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert tools == {"create_calendar_event", "batch_modify_gmail_thread_labels", "create_gmail_draft"}
    assert GOOGLE_SERVER_ID == "google"


async def test_create_calendar_event_tool_dispatches_to_calendar_client():
    calendar = Mock()
    calendar.create_event.return_value = CreateCalendarEventResult(event_id="evt1", html_link="https://x/evt1")
    async with Client(_mcp(calendar=calendar)) as client:
        result = await client.call_tool(
            "create_calendar_event", {"summary": "S", "start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}
        )
    assert not result.is_error
    assert result.data.event_id == "evt1"
    calendar.create_event.assert_called_once()
    (args,), _kwargs = calendar.create_event.call_args
    assert args.summary == "S"


async def test_batch_modify_gmail_thread_labels_tool_dispatches_to_gmail_client():
    gmail = Mock()
    gmail.batch_modify_thread_labels.return_value = BatchModifyGmailThreadLabelsResult(
        added=[], removed=[], thread_count=1
    )
    async with Client(_mcp(gmail=gmail)) as client:
        result = await client.call_tool("batch_modify_gmail_thread_labels", {"thread_ids": ["t1"], "add": ["urgent"]})
    assert not result.is_error
    assert result.data.thread_count == 1
    (args,), _kwargs = gmail.batch_modify_thread_labels.call_args
    assert args.thread_ids == ["t1"]
    assert args.add == ["urgent"]


async def test_batch_modify_gmail_thread_labels_tool_omits_empty_add_remove():
    gmail = Mock()
    gmail.batch_modify_thread_labels.return_value = BatchModifyGmailThreadLabelsResult(
        added=[], removed=[], thread_count=1
    )
    async with Client(_mcp(gmail=gmail)) as client:
        await client.call_tool("batch_modify_gmail_thread_labels", {"thread_ids": ["t1"], "add": ["urgent"]})
    (args,), _kwargs = gmail.batch_modify_thread_labels.call_args
    assert args.remove == []  # `remove` omitted entirely from the call -> defaults to empty, not None


async def test_create_gmail_draft_tool_dispatches_to_gmail_client():
    gmail = Mock()
    gmail.create_draft.return_value = CreateGmailDraftResult(draft_id="d1", message_id="m1")
    async with Client(_mcp(gmail=gmail)) as client:
        result = await client.call_tool("create_gmail_draft", {"to": ["a@example.com"], "subject": "S", "body": "B"})
    assert not result.is_error
    assert result.data.draft_id == "d1"


async def test_create_gmail_draft_tool_rejects_empty_recipients():
    async with Client(_mcp()) as client:
        result = await client.call_tool(
            "create_gmail_draft", {"to": [], "subject": "S", "body": "B"}, raise_on_error=False
        )
    assert result.is_error


def test_gmail_thread_previews_endpoint_503s_when_unconfigured(make_client) -> None:
    with make_client() as client:
        resp = client.get("/api/google/gmail/thread-previews", params={"thread_id": ["t1"]})
        assert resp.status_code == 503


def test_gmail_thread_previews_endpoint_composes_preview_over_the_client_service(make_client) -> None:
    previews = {
        "t1": GmailThreadPreview(subject="Test", snippet="hi", current_label_names=["haku/x"], gmail_url="https://x/t1")
    }
    gmail = Mock()  # non-None so the endpoint doesn't 503; its .service is handed to the reader
    with (
        patch("haku.console.tools.google.preview_gmail_threads", return_value=previews) as preview_mock,
        make_client(google_gmail_client=gmail) as client,
    ):
        resp = client.get("/api/google/gmail/thread-previews", params={"thread_id": ["t1"]})
    assert resp.status_code == 200
    assert resp.json()["threads"]["t1"]["subject"] == "Test"
    preview_mock.assert_called_once_with(gmail.service, ["t1"])


if __name__ == "__main__":
    pytest_bazel.main()
