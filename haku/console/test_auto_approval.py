"""Unit tests for reviewed haku-console auto-approval policies."""

from unittest.mock import Mock

import pytest
import pytest_bazel

from gmail_api.labels import GmailLabel, LabelType
from haku.console.auto_approval import GMAIL_LABEL_POLICY_ID, evaluate_auto_approval


async def _decision(tool_name: str, arguments: dict, *, gmail=None, caller="haku-agent-api-token"):
    return await evaluate_auto_approval(
        caller_principal=caller,
        server_id="gmail",
        tool_name=tool_name,
        arguments=arguments,
        label_prefix="haku/",
        gmail=gmail,
    )


async def test_lists_all_labels_without_namespace_filter() -> None:
    decision = await _decision("labels_list", {})
    assert decision is not None
    assert decision.policy_id == GMAIL_LABEL_POLICY_ID


@pytest.mark.parametrize("field", ["add", "remove"])
async def test_modifies_only_namespaced_labels(field: str) -> None:
    assert await _decision("threads_batch_modify", {"thread_ids": ["t1"], field: ["haku/triaged"]})
    assert await _decision("threads_batch_modify", {"thread_ids": ["t1"], field: ["INBOX"]}) is None


async def test_modify_rejects_unknown_arguments() -> None:
    assert (
        await _decision("threads_batch_modify", {"thread_ids": ["t1"], "add": ["haku/triaged"], "unexpected": True})
        is None
    )


async def test_patch_requires_old_and_new_names_in_namespace() -> None:
    gmail = Mock()
    gmail.labels_get.return_value = GmailLabel(id="Label_1", name="haku/old", type=LabelType.USER)
    assert await _decision("labels_patch", {"label_id": "Label_1", "name": "haku/new"}, gmail=gmail)
    assert await _decision("labels_patch", {"label_id": "Label_1", "name": "other"}, gmail=gmail) is None

    gmail.labels_get.return_value = GmailLabel(id="Label_2", name="other", type=LabelType.USER)
    assert await _decision("labels_patch", {"label_id": "Label_2", "name": "haku/new"}, gmail=gmail) is None


async def test_patch_visibility_change_stays_manual() -> None:
    gmail = Mock()
    gmail.labels_get.return_value = GmailLabel(id="Label_1", name="haku/x", type=LabelType.USER)
    assert (
        await _decision("labels_patch", {"label_id": "Label_1", "label_list_visibility": "labelHide"}, gmail=gmail)
        is None
    )
    gmail.labels_get.assert_not_called()


async def test_delete_resolves_existing_label_name() -> None:
    gmail = Mock()
    gmail.labels_get.return_value = GmailLabel(id="Label_1", name="haku/x", type=LabelType.USER)
    assert await _decision("labels_delete", {"label_id": "Label_1"}, gmail=gmail)
    gmail.labels_get.return_value = GmailLabel(id="INBOX", name="INBOX", type=LabelType.SYSTEM)
    assert await _decision("labels_delete", {"label_id": "INBOX"}, gmail=gmail) is None


async def test_only_haku_agent_principal_is_auto_approved() -> None:
    assert await _decision("labels_list", {}, caller="operator") is None


async def test_lookup_errors_are_logged_and_fail_closed(caplog: pytest.LogCaptureFixture) -> None:
    gmail = Mock()
    gmail.labels_get.side_effect = RuntimeError("gmail unavailable")
    with caplog.at_level("ERROR"):
        assert await _decision("labels_delete", {"label_id": "Label_1"}, gmail=gmail) is None
    assert "auto-approval policy evaluation failed" in caplog.text
    assert "gmail unavailable" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
