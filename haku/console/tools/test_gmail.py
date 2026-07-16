"""Tests for the in-process `gmail` MCP server (build_mcp).

The GmailToolsClient (the network-facing seam) is a Mock; every value it returns is a real
`gmail_api` resource model, so the tool layer is exercised against genuine resources.
"""

from unittest.mock import Mock

import pytest
import pytest_bazel
from fastmcp import Client

from gmail_api.filters import FilterAction, FilterCriteria, GmailFilter
from gmail_api.labels import GmailLabel, LabelsListResponse, LabelType
from gmail_api.messages import (
    Draft,
    DraftsListResponse,
    Message,
    MessageFormat,
    Thread,
    ThreadFormat,
    ThreadsListResponse,
)
from haku.console.tools.gmail import GMAIL_SERVER_ID, build_mcp
from haku.console.tools.gmail_client import ModifyGmailThreadLabelsResult


@pytest.fixture
def gmail() -> Mock:
    return Mock()


@pytest.fixture
async def client(gmail: Mock):
    # build_mcp holds the same `gmail` mock, so a test configures `gmail.<tool>.return_value`
    # (after this fixture runs) and the tool call picks it up at call time.
    async with Client(build_mcp(gmail)) as mcp_client:
        yield mcp_client


async def test_tool_surface(client):
    tools = {tool.name for tool in await client.list_tools()}
    assert tools == {
        "threads_list",
        "threads_get",
        "messages_get",
        "labels_list",
        "labels_get",
        "threads_modify_labels",
        "drafts_create",
        "labels_create",
        "labels_patch",
        "labels_delete",
        "filters_list",
        "filters_get",
        "filters_create",
        "filters_delete",
        "drafts_list",
        "drafts_get",
        "drafts_update",
        "drafts_delete",
    }
    assert GMAIL_SERVER_ID == "gmail"


async def test_threads_list_dispatches_with_pagination_args(gmail: Mock, client):
    gmail.threads_list.return_value = ThreadsListResponse(threads=[Thread(id="t1")], next_page_token="N")
    result = await client.call_tool("threads_list", {"query": "from:a", "max_results": 5, "page_token": "P"})
    assert not result.is_error
    # FastMCP serializes the result with the model's aliases, so the wire shape is Gmail's
    # own camelCase (`nextPageToken`) — the point of mirroring the API.
    assert result.data.nextPageToken == "N"
    (args,), _kwargs = gmail.threads_list.call_args
    assert args.query == "from:a"
    assert args.max_results == 5
    assert args.page_token == "P"


async def test_threads_get_dispatches_with_format(gmail: Mock, client):
    gmail.threads_get.return_value = Thread(id="t1")
    result = await client.call_tool("threads_get", {"thread_id": "t1", "format": "metadata"})
    assert not result.is_error
    gmail.threads_get.assert_called_once_with("t1", ThreadFormat.METADATA)


async def test_threads_get_defaults_to_full(gmail: Mock, client):
    gmail.threads_get.return_value = Thread(id="t1")
    await client.call_tool("threads_get", {"thread_id": "t1"})
    gmail.threads_get.assert_called_once_with("t1", ThreadFormat.FULL)


async def test_messages_get_dispatches_with_raw_format(gmail: Mock, client):
    gmail.messages_get.return_value = Message(id="m1", raw="UkFX")
    result = await client.call_tool("messages_get", {"message_id": "m1", "format": "raw"})
    assert not result.is_error
    assert result.data.raw == "UkFX"
    gmail.messages_get.assert_called_once_with("m1", MessageFormat.RAW)


async def test_labels_list_dispatches(gmail: Mock, client):
    gmail.labels_list.return_value = LabelsListResponse(labels=[GmailLabel(id="L1", name="x", type=LabelType.USER)])
    result = await client.call_tool("labels_list", {})
    assert not result.is_error
    gmail.labels_list.assert_called_once_with()


async def test_labels_get_dispatches(gmail: Mock, client):
    gmail.labels_get.return_value = GmailLabel(id="L1", name="x", type=LabelType.USER)
    await client.call_tool("labels_get", {"label_id": "L1"})
    gmail.labels_get.assert_called_once_with("L1")


async def test_threads_modify_labels_dispatches(gmail: Mock, client):
    gmail.threads_modify_labels.return_value = ModifyGmailThreadLabelsResult(added=[], removed=[], thread_count=1)
    result = await client.call_tool("threads_modify_labels", {"thread_ids": ["t1"], "add": ["urgent"]})
    assert not result.is_error
    (args,), _kwargs = gmail.threads_modify_labels.call_args
    assert args.thread_ids == ["t1"]
    assert args.add == ["urgent"]
    assert args.remove == []  # omitted -> empty, not None


async def test_threads_modify_labels_accepts_explicit_null_list(gmail: Mock, client):
    gmail.threads_modify_labels.return_value = ModifyGmailThreadLabelsResult(added=[], removed=[], thread_count=1)
    result = await client.call_tool("threads_modify_labels", {"thread_ids": ["t1"], "add": ["urgent"], "remove": None})
    assert not result.is_error
    (args,), _kwargs = gmail.threads_modify_labels.call_args
    assert args.add == ["urgent"]
    assert args.remove == []


async def test_threads_modify_labels_rejects_unknown_arguments(client):
    result = await client.call_tool(
        "threads_modify_labels", {"thread_ids": ["t1"], "add": ["haku/x"], "unexpected": True}, raise_on_error=False
    )
    assert result.is_error


async def test_drafts_create_returns_draft_resource(gmail: Mock, client):
    gmail.drafts_create.return_value = Draft(id="d1", message=Message(id="m1"))
    result = await client.call_tool("drafts_create", {"to": ["a@example.com"], "subject": "S", "body": "B"})
    assert not result.is_error
    assert result.data.id == "d1"


async def test_drafts_create_accepts_explicit_null_cc(gmail: Mock, client):
    gmail.drafts_create.return_value = Draft(id="d1", message=Message(id="m1"))
    result = await client.call_tool("drafts_create", {"to": ["a@example.com"], "subject": "S", "body": "B", "cc": None})
    assert not result.is_error
    (args,), _kwargs = gmail.drafts_create.call_args
    assert args.cc == []


async def test_drafts_create_rejects_empty_recipients(client):
    result = await client.call_tool("drafts_create", {"to": [], "subject": "S", "body": "B"}, raise_on_error=False)
    assert result.is_error


async def test_labels_create_dispatches_request(gmail: Mock, client):
    gmail.labels_create.return_value = GmailLabel(id="L1", name="receipts", type=LabelType.USER)
    await client.call_tool("labels_create", {"name": "receipts"})
    (request,), _kwargs = gmail.labels_create.call_args
    assert request.name == "receipts"


async def test_labels_patch_dispatches_partial_request(gmail: Mock, client):
    gmail.labels_patch.return_value = GmailLabel(id="L1", name="renamed", type=LabelType.USER)
    await client.call_tool("labels_patch", {"label_id": "L1", "name": "renamed"})
    (label_id, request), _kwargs = gmail.labels_patch.call_args
    assert label_id == "L1"
    assert request.name == "renamed"
    assert request.label_list_visibility is None  # unset -> stays None (partial patch)


async def test_labels_delete_dispatches_and_returns_no_error(gmail: Mock, client):
    result = await client.call_tool("labels_delete", {"label_id": "L9"}, raise_on_error=False)
    assert not result.is_error
    gmail.labels_delete.assert_called_once_with("L9")


async def test_filters_create_forwards_nested_criteria_and_action(gmail: Mock, client):
    gmail.filters_create.return_value = GmailFilter(
        id="F1", criteria=FilterCriteria(from_="a@x"), action=FilterAction(add_label_ids=["L1"])
    )
    result = await client.call_tool("filters_create", {"criteria": {"from": "a@x"}, "action": {"addLabelIds": ["L1"]}})
    assert not result.is_error
    (criteria, action), _kwargs = gmail.filters_create.call_args
    assert criteria.from_ == "a@x"  # camelCase wire `from` -> python `from_`
    assert action.add_label_ids == ["L1"]


async def test_drafts_list_dispatches_with_pagination_args(gmail: Mock, client):
    gmail.drafts_list.return_value = DraftsListResponse(
        drafts=[Draft(id="d1", message=Message(id="m1"))], next_page_token="N"
    )
    result = await client.call_tool("drafts_list", {"query": "receipts", "max_results": 5, "page_token": "P"})
    assert not result.is_error
    assert result.data.nextPageToken == "N"
    (args,), _kwargs = gmail.drafts_list.call_args
    assert args.query == "receipts"
    assert args.max_results == 5
    assert args.page_token == "P"


async def test_drafts_get_defaults_to_minimal(gmail: Mock, client):
    gmail.drafts_get.return_value = Draft(id="d1", message=Message(id="m1"))
    await client.call_tool("drafts_get", {"draft_id": "d1"})
    gmail.drafts_get.assert_called_once_with("d1", MessageFormat.MINIMAL)


async def test_drafts_update_dispatches_with_draft_id(gmail: Mock, client):
    gmail.drafts_update.return_value = Draft(id="d9", message=Message(id="m1"))
    result = await client.call_tool("drafts_update", {"draft_id": "d9", "to": ["a@x"], "subject": "S", "body": "B"})
    assert not result.is_error
    (args,), _kwargs = gmail.drafts_update.call_args
    assert args.draft_id == "d9"
    assert args.to == ["a@x"]


if __name__ == "__main__":
    pytest_bazel.main()
