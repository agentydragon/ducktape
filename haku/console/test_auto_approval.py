"""Unit tests for haku-console's reviewed auto-approval decision."""

from unittest.mock import Mock
from uuid import UUID

import pytest
import pytest_bazel

from gmail_api.labels import GmailLabel, LabelType
from haku.console.auto_approval import UNCONDITIONAL_AUTO_APPROVAL_ID, SchemaDenial, auto_approve_tool_call
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tools.gmail import build_mcp
from haku.console.tools.google_calendar import build_mcp as build_calendar_mcp

TEST_OPERATOR_ID = UUID("00000000-0000-0000-0000-000000000001")
AGENT_ACTOR = AgentActor(
    agent_id=UUID("00000000-0000-0000-0000-000000000002"),
    operator_id=TEST_OPERATOR_ID,
    binding_id=UUID("00000000-0000-0000-0000-000000000003"),
)
OPERATOR_ACTOR = OperatorActor(operator_id=TEST_OPERATOR_ID)


async def _decision(tool_name: str, arguments: dict, *, gmail=None, actor: ToolCallActor = AGENT_ACTOR):
    gmail = gmail or Mock()
    return await auto_approve_tool_call(
        actor=actor,
        server_id="gmail",
        tool_name=tool_name,
        arguments=arguments,
        label_prefix="haku/",
        gmail=gmail,
        mcp=build_mcp(gmail),
    )


def _approval(decision: tuple[str | None, str | None] | SchemaDenial) -> tuple[str | None, str | None]:
    """Unwrap a decision the test expects NOT to be a terminal schema denial."""
    assert not isinstance(decision, SchemaDenial), decision
    return decision


async def _policy_id(tool_name: str, arguments: dict, **kwargs):
    policy_id, _evaluation = _approval(await _decision(tool_name, arguments, **kwargs))
    return policy_id


async def _calendar_decision(tool_name: str, arguments: dict) -> tuple[str | None, str | None] | SchemaDenial:
    calendar = Mock()
    return await auto_approve_tool_call(
        actor=AGENT_ACTOR,
        server_id="google_calendar",
        tool_name=tool_name,
        arguments=arguments,
        label_prefix="haku/",
        gmail=None,
        mcp=build_calendar_mcp(calendar),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("threads_list", {"q": "from:alice", "maxResults": 50}),
        ("threads_get", {"id": "t1", "format": "full"}),
        ("messages_get", {"id": "m1", "format": "raw"}),
        ("labels_list", {}),
        ("labels_get", {"id": "INBOX"}),
        ("filters_list", {}),
        ("filters_get", {"id": "F1"}),
        ("drafts_list", {}),
        ("drafts_get", {"id": "d1"}),
    ],
)
async def test_all_gmail_reads_are_auto_approved(tool_name: str, arguments: dict) -> None:
    policy_id, evaluation = await _decision(tool_name, arguments)
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("filters_create", {"criteria": {"from": "a@x"}, "action": {"addLabelIds": ["L1"]}}),
        ("filters_delete", {"filter_id": "F9"}),
        ("drafts_update", {"draft_id": "d9", "to": ["a@x"], "subject": "S", "body": "B"}),
        ("drafts_delete", {"draft_id": "d9"}),
    ],
)
async def test_gmail_writes_stay_manual(tool_name: str, arguments: dict) -> None:
    policy_id, _evaluation = await _decision(tool_name, arguments)
    assert policy_id is None


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_event", {"event_id": "evt1"}),
        ("list_events", {"expand_recurring": True, "max_results": 50}),
        ("list_event_instances", {"recurring_event_id": "series1"}),
    ],
)
async def test_calendar_reads_are_auto_approved(tool_name: str, arguments: dict) -> None:
    policy_id, evaluation = _approval(await _calendar_decision(tool_name, arguments))
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


async def test_calendar_create_stays_manual() -> None:
    policy_id, evaluation = _approval(
        await _calendar_decision(
            "create_event", {"summary": "Standup", "start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}
        )
    )
    assert policy_id is None
    assert evaluation == "manual: google_calendar/create_event is not auto-approved"


async def test_calendar_read_with_invalid_arguments_is_auto_denied() -> None:
    denial = await _calendar_decision("list_events", {"max_results": 251})
    assert isinstance(denial, SchemaDenial)
    assert denial.evaluation == "denied: arguments failed the registered tool schema"
    assert "251" in denial.reason  # the concrete validation error reaches the caller


async def test_read_with_unknown_argument_is_auto_denied() -> None:
    denial = await _decision("threads_list", {"q": "", "unexpected": True})
    assert isinstance(denial, SchemaDenial)
    assert "unexpected" in denial.reason


@pytest.mark.parametrize("field", ["add", "remove"])
async def test_modifies_only_namespaced_labels(field: str) -> None:
    assert await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], field: ["haku/triaged"]})
    assert await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], field: ["INBOX"]}) is None


async def test_modify_rejects_unknown_arguments() -> None:
    denial = await _decision(
        "threads_modify_labels", {"thread_ids": ["t1"], "add": ["haku/triaged"], "unexpected": True}
    )
    assert isinstance(denial, SchemaDenial)
    assert "unexpected" in denial.reason


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


async def test_operator_actor_is_not_auto_approved() -> None:
    assert await _decision("labels_list", {}, actor=OPERATOR_ACTOR) == (None, None)


async def _remote_decision(server_id: str, tool_name: str, arguments: dict) -> tuple[str | None, str | None]:
    # Remote (operator_oauth) servers have no in-process schema, so `mcp` is None.
    return _approval(
        await auto_approve_tool_call(
            actor=AGENT_ACTOR,
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
            label_prefix="haku/",
            gmail=None,
            mcp=None,
        )
    )


async def test_grocy_reads_auto_approve() -> None:
    policy_id, evaluation = await _remote_decision("grocy-sf", "products_list", {"detail": "brief"})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


async def test_grocy_writes_stay_manual() -> None:
    assert await _remote_decision("grocy-sf", "products_create", {"name": "Milk"}) == (
        None,
        "manual: grocy-sf/products_create is not auto-approved",
    )


async def test_tana_calendar_node_auto_approves() -> None:
    policy_id, _ = await _remote_decision("tana-rw", "get_or_create_calendar_node", {"date": "2026-07-12"})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID


@pytest.mark.parametrize(
    "tool_name",
    ["search_nodes", "read_node", "get_children", "open_node", "list_tags", "list_workspaces", "get_tag_schema"],
)
async def test_tana_reads_auto_approve(tool_name: str) -> None:
    policy_id, evaluation = await _remote_decision("tana-rw", tool_name, {})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


async def test_tana_writes_stay_manual() -> None:
    policy_id, _ = await _remote_decision("tana-rw", "create_tag", {"name": "x"})
    assert policy_id is None


@pytest.mark.parametrize("tool_name", ["market_data_snapshot", "session_status", "request_reauth"])
async def test_ibkr_reads_auto_approve(tool_name: str) -> None:
    policy_id, evaluation = await _remote_decision("interactive_brokers", tool_name, {})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


async def test_ibkr_unlisted_tool_stays_manual() -> None:
    # The allowlist is explicit, not "everything under interactive_brokers": a tool the server would
    # never expose (it has no order routes) still would not auto-approve.
    assert await _remote_decision("interactive_brokers", "place_order", {}) == (
        None,
        "manual: interactive_brokers/place_order is not auto-approved",
    )


@pytest.mark.parametrize("tool_name", ["geocode_address", "route_fetch", "find_nearby_places", "tile_cache"])
async def test_osm_reads_auto_approve(tool_name: str) -> None:
    policy_id, evaluation = await _remote_decision("osm", tool_name, {})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


async def test_osm_unlisted_tool_stays_manual() -> None:
    assert await _remote_decision("osm", "not_a_real_tool", {}) == (
        None,
        "manual: osm/not_a_real_tool is not auto-approved",
    )


@pytest.mark.parametrize(
    "tool_name", ["ha_get_state", "ha_search", "ha_get_history", "ha_eval_template", "ha_get_overview"]
)
async def test_home_assistant_reads_auto_approve(tool_name: str) -> None:
    policy_id, evaluation = await _remote_decision("home-assistant", tool_name, {})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


@pytest.mark.parametrize(
    "tool_name", ["ha_call_service", "ha_set_state", "ha_config_set_automation", "ha_restart", "ha_report_issue"]
)
async def test_home_assistant_writes_and_side_effects_stay_manual(tool_name: str) -> None:
    # Control/config-write tools stay gated; ha_report_issue is annotated read-only upstream but
    # files an issue outward, so it is deliberately excluded from the read allowlist.
    policy_id, evaluation = await _remote_decision("home-assistant", tool_name, {})
    assert policy_id is None
    assert evaluation == f"manual: home-assistant/{tool_name} is not auto-approved"


@pytest.mark.parametrize("tool_name", ["list_items", "list_automation_rules"])
async def test_postscanmail_reads_auto_approve(tool_name: str) -> None:
    policy_id, evaluation = await _remote_decision("postscanmail-mcp", tool_name, {"page": 1})
    assert policy_id == UNCONDITIONAL_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "allowlisted" in evaluation


@pytest.mark.parametrize(
    "tool_name", ["set_automation_rule", "request_open", "request_discard", "request_shred", "cancel_shred"]
)
async def test_postscanmail_writes_stay_manual(tool_name: str) -> None:
    # Every mutating/paid/destructive tool stays approval-gated — only the two GET reads
    # auto-approve. Covers a paid scan (request_open), the destructive pair (discard/shred),
    # the automation toggle, and a cancel.
    assert await _remote_decision("postscanmail-mcp", tool_name, {}) == (
        None,
        f"manual: postscanmail-mcp/{tool_name} is not auto-approved",
    )


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


if __name__ == "__main__":
    pytest_bazel.main()
