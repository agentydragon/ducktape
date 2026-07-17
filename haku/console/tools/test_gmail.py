"""Tests for the in-process `gmail` MCP server (build_mcp).

Reads are generated (`google_discovery.py`) and dispatch through the raw `googleapiclient`
service (`gmail.service`), so read tests stub that mock chain and assert the Google-native call.
Writes go through the hand-written `GmailToolsClient`, so write tests mock its methods and pass
`gmail_api` resource models back.
"""

from unittest.mock import Mock

import pytest
import pytest_bazel
from fastmcp import Client

from gmail_api.filters import FilterAction, FilterCriteria, GmailFilter
from gmail_api.labels import GmailLabel, LabelType
from gmail_api.messages import Draft, Message
from haku.console.tools.gmail import GMAIL_SERVER_ID, build_mcp
from haku.console.tools.gmail_client import ModifyGmailThreadLabelsResult


@pytest.fixture
def gmail() -> Mock:
    return Mock()


@pytest.fixture
async def client(gmail: Mock):
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


# --- generated reads: Google-native params, pinned userId=me, verbatim result ---
async def test_threads_list_dispatches_with_pagination_args(gmail: Mock, client):
    threads = gmail.service.users.return_value.threads.return_value
    threads.list.return_value.execute.return_value = {"threads": [{"id": "t1"}], "nextPageToken": "N"}
    result = await client.call_tool("threads_list", {"q": "from:a", "maxResults": 5, "pageToken": "P"})
    assert not result.is_error
    assert result.structured_content["nextPageToken"] == "N"  # raw Gmail camelCase, verbatim
    threads.list.assert_called_once_with(userId="me", q="from:a", maxResults=5, pageToken="P")


async def test_threads_get_dispatches_with_format(gmail: Mock, client):
    threads = gmail.service.users.return_value.threads.return_value
    threads.get.return_value.execute.return_value = {"id": "t1"}
    result = await client.call_tool("threads_get", {"id": "t1", "format": "metadata"})
    assert not result.is_error
    threads.get.assert_called_once_with(userId="me", id="t1", format="metadata")


async def test_threads_get_omits_format_when_unset(gmail: Mock, client):
    threads = gmail.service.users.return_value.threads.return_value
    threads.get.return_value.execute.return_value = {"id": "t1"}
    await client.call_tool("threads_get", {"id": "t1"})
    threads.get.assert_called_once_with(userId="me", id="t1")  # no client default; Gmail chooses


async def test_messages_get_dispatches_with_raw_format(gmail: Mock, client):
    messages = gmail.service.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = {"id": "m1", "raw": "UkFX"}
    result = await client.call_tool("messages_get", {"id": "m1", "format": "raw"})
    assert not result.is_error
    assert result.structured_content["raw"] == "UkFX"
    messages.get.assert_called_once_with(userId="me", id="m1", format="raw")


async def test_labels_list_dispatches(gmail: Mock, client):
    labels = gmail.service.users.return_value.labels.return_value
    labels.list.return_value.execute.return_value = {"labels": [{"id": "L1", "name": "x", "type": "user"}]}
    result = await client.call_tool("labels_list", {})
    assert not result.is_error
    labels.list.assert_called_once_with(userId="me")


async def test_labels_get_dispatches(gmail: Mock, client):
    labels = gmail.service.users.return_value.labels.return_value
    labels.get.return_value.execute.return_value = {"id": "L1", "name": "x", "type": "user"}
    await client.call_tool("labels_get", {"id": "L1"})
    labels.get.assert_called_once_with(userId="me", id="L1")


async def test_filters_get_dispatches(gmail: Mock, client):
    filters = gmail.service.users.return_value.settings.return_value.filters.return_value
    filters.get.return_value.execute.return_value = {"id": "F1"}
    await client.call_tool("filters_get", {"id": "F1"})
    filters.get.assert_called_once_with(userId="me", id="F1")


async def test_drafts_list_dispatches_with_pagination_args(gmail: Mock, client):
    drafts = gmail.service.users.return_value.drafts.return_value
    drafts.list.return_value.execute.return_value = {"drafts": [{"id": "d1"}], "nextPageToken": "N"}
    result = await client.call_tool("drafts_list", {"q": "receipts", "maxResults": 5, "pageToken": "P"})
    assert not result.is_error
    assert result.structured_content["nextPageToken"] == "N"
    drafts.list.assert_called_once_with(userId="me", q="receipts", maxResults=5, pageToken="P")


async def test_drafts_get_omits_format_when_unset(gmail: Mock, client):
    drafts = gmail.service.users.return_value.drafts.return_value
    drafts.get.return_value.execute.return_value = {"id": "d1"}
    await client.call_tool("drafts_get", {"id": "d1"})
    drafts.get.assert_called_once_with(userId="me", id="d1")


async def test_read_rejects_unknown_argument(client):
    result = await client.call_tool("threads_list", {"q": "x", "unexpected": True}, raise_on_error=False)
    assert result.is_error  # generated schema has additionalProperties: False


# --- hand-written writes: unchanged (GmailToolsClient, friendly args) ---
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


async def test_drafts_update_dispatches_with_draft_id(gmail: Mock, client):
    gmail.drafts_update.return_value = Draft(id="d9", message=Message(id="m1"))
    result = await client.call_tool("drafts_update", {"draft_id": "d9", "to": ["a@x"], "subject": "S", "body": "B"})
    assert not result.is_error
    (args,), _kwargs = gmail.drafts_update.call_args
    assert args.draft_id == "d9"
    assert args.to == ["a@x"]


if __name__ == "__main__":
    pytest_bazel.main()
