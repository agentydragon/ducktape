"""Unit tests for haku-console's reviewed auto-approval decision."""

from unittest.mock import Mock

import pytest
import pytest_bazel

from gmail_api.labels import GmailLabel, LabelType
from haku.console.auto_approval import (
    GMAIL_AUTO_APPROVAL_ID,
    GROCY_READ_TOOLS,
    GROCY_SF_SERVER_ID,
    TANA_RW_SERVER_ID,
    UNCONDITIONAL_AUTO_APPROVAL_ID,
    auto_approve_tool_call,
)
from haku.console.tools.gmail import build_mcp


async def _decision(tool_name: str, arguments: dict, *, gmail=None, caller="haku-agent-api-token"):
    gmail = gmail or Mock()
    return await auto_approve_tool_call(
        caller_principal=caller,
        server_id="gmail",
        tool_name=tool_name,
        arguments=arguments,
        label_prefix="haku/",
        gmail=gmail,
        mcp=build_mcp(gmail),
    )


async def _policy_id(tool_name: str, arguments: dict, **kwargs):
    policy_id, _evaluation = await _decision(tool_name, arguments, **kwargs)
    return policy_id


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("threads_list", {"query": "from:alice", "max_results": 50}),
        ("threads_get", {"thread_id": "t1", "format": "full"}),
        ("messages_get", {"message_id": "m1", "format": "raw"}),
        ("labels_list", {}),
        ("labels_get", {"label_id": "INBOX"}),
    ],
)
async def test_all_gmail_reads_are_auto_approved(tool_name: str, arguments: dict) -> None:
    policy_id, evaluation = await _decision(tool_name, arguments)
    assert policy_id == GMAIL_AUTO_APPROVAL_ID
    assert evaluation == "approved: Gmail search/read operation"


async def test_read_requires_valid_registered_tool_arguments() -> None:
    policy_id, evaluation = await _decision("threads_list", {"query": "", "unexpected": True})
    assert policy_id is None
    assert evaluation == "manual: arguments failed the registered Gmail tool schema"


@pytest.mark.parametrize("field", ["add", "remove"])
async def test_modifies_only_namespaced_labels(field: str) -> None:
    assert await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], field: ["haku/triaged"]})
    assert await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], field: ["INBOX"]}) is None


async def test_modify_rejects_unknown_arguments() -> None:
    assert (
        await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], "add": ["haku/triaged"], "unexpected": True})
        is None
    )


async def test_patch_requires_old_and_new_names_in_namespace() -> None:
    gmail = Mock()
    gmail.labels_get.return_value = GmailLabel(id="Label_1", name="haku/old", type=LabelType.USER)
    assert await _policy_id("labels_patch", {"label_id": "Label_1", "name": "haku/new"}, gmail=gmail)
    assert await _policy_id("labels_patch", {"label_id": "Label_1", "name": "other"}, gmail=gmail) is None

    gmail.labels_get.return_value = GmailLabel(id="Label_2", name="other", type=LabelType.USER)
    assert await _policy_id("labels_patch", {"label_id": "Label_2", "name": "haku/new"}, gmail=gmail) is None


async def test_patch_visibility_change_stays_manual() -> None:
    gmail = Mock()
    gmail.labels_get.return_value = GmailLabel(id="Label_1", name="haku/x", type=LabelType.USER)
    assert (
        await _policy_id("labels_patch", {"label_id": "Label_1", "label_list_visibility": "labelHide"}, gmail=gmail)
        is None
    )
    gmail.labels_get.assert_not_called()


async def test_delete_resolves_existing_label_name() -> None:
    gmail = Mock()
    gmail.labels_get.return_value = GmailLabel(id="Label_1", name="haku/x", type=LabelType.USER)
    assert await _policy_id("labels_delete", {"label_id": "Label_1"}, gmail=gmail)
    gmail.labels_get.return_value = GmailLabel(id="INBOX", name="INBOX", type=LabelType.SYSTEM)
    assert await _policy_id("labels_delete", {"label_id": "INBOX"}, gmail=gmail) is None


async def test_only_haku_agent_principal_is_auto_approved() -> None:
    assert await _decision("labels_list", {}, caller="operator") == (None, None)


async def test_lookup_errors_are_logged_and_fail_closed(caplog: pytest.LogCaptureFixture) -> None:
    gmail = Mock()
    gmail.labels_get.side_effect = RuntimeError("gmail unavailable")
    with caplog.at_level("ERROR"):
        assert await _decision("labels_delete", {"label_id": "Label_1"}, gmail=gmail) == (
            None,
            "error: Gmail auto-approval evaluation failed",
        )
    assert "auto-approval evaluation failed" in caplog.text
    assert "gmail unavailable" in caplog.text


async def _remote_decision(server_id: str, tool_name: str, arguments: dict, *, caller: str = "haku-agent-api-token"):
    # Remote (operator_oauth) servers have no in-process schema, so gmail/mcp are unused.
    return await auto_approve_tool_call(
        caller_principal=caller,
        server_id=server_id,
        tool_name=tool_name,
        arguments=arguments,
        label_prefix="haku/",
        gmail=None,
        mcp=None,
    )


@pytest.mark.parametrize("tool_name", sorted(GROCY_READ_TOOLS))
async def test_grocy_reads_are_auto_approved(tool_name: str) -> None:
    policy_id, evaluation = await _remote_decision(GROCY_SF_SERVER_ID, tool_name, {})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "read-only/safe" in evaluation


async def test_grocy_mutations_are_not_auto_approved() -> None:
    # A mutating grocy tool is not in the read allowlist, so it stays manual (grocy-sf ≠ gmail path).
    assert await _remote_decision(GROCY_SF_SERVER_ID, "stock_add", {"items": []}) == (None, None)


async def test_tana_calendar_node_is_auto_approved() -> None:
    policy_id, _evaluation = await _remote_decision(
        TANA_RW_SERVER_ID, "get_or_create_calendar_node", {"date": "2026-07-12"}
    )
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID


async def test_tana_other_tools_are_not_auto_approved() -> None:
    assert await _remote_decision(TANA_RW_SERVER_ID, "create_tag", {"name": "X"}) == (None, None)


async def test_remote_allowlist_only_for_haku_agent() -> None:
    assert await _remote_decision(GROCY_SF_SERVER_ID, "stock_get", {}, caller="operator") == (None, None)


if __name__ == "__main__":
    pytest_bazel.main()
