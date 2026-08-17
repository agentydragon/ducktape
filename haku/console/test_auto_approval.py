"""Unit tests for haku-console's reviewed auto-approval decision."""

from unittest.mock import Mock
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from gmail_api.labels import GmailLabel, LabelType
from haku.console.auto_approval import (
    AGENT_AUTO_APPROVAL_ID,
    AutoApprovalPolicyRegistry,
    SchemaDenial,
    ToolAutoApprovalMode,
    auto_approve_tool_call,
)
from haku.console.mcp_config import ConsoleConfigFile
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

_EXACT_TOOLS = {
    "gmail": [
        "threads_list",
        "threads_get",
        "messages_get",
        "labels_list",
        "labels_get",
        "filters_list",
        "filters_get",
        "drafts_list",
        "drafts_get",
    ],
    "google_calendar": ["get_event", "list_events", "list_event_instances"],
    "grocy-sf": ["products_list"],
}
_SERVER_CONFIGS = [
    {
        "id": server_id,
        "backend": {"kind": "remote_mcp", "url": f"https://{server_id}.test/mcp", "auth": {"kind": "none"}},
    }
    for server_id in _EXACT_TOOLS
] + [{"id": "github", "backend": {"kind": "remote_mcp", "url": "https://github.test/mcp", "auth": {"kind": "none"}}}]
_GITHUB_TOOLS = [
    "actions_get",
    "actions_list",
    "get_file_contents",
    "get_job_logs",
    "issue_read",
    "list_issues",
    "list_pull_requests",
    "pull_request_read",
]
_POLICIES = AutoApprovalPolicyRegistry(
    ConsoleConfigFile.model_validate(
        {
            "mcp": {"servers": _SERVER_CONFIGS},
            "auto_approval_policies": [
                {"id": "safe_tools", "type": "exact_tools", "tools": _EXACT_TOOLS},
                {
                    "id": "managed_gmail_labels",
                    "type": "gmail_label_namespace",
                    "server": "gmail",
                    "label_prefix": "haku/",
                },
                {
                    "id": "public_ducktape_reads",
                    "type": "github_repository",
                    "server": "github",
                    "owner": "agentydragon",
                    "repository": "ducktape",
                    "tools": _GITHUB_TOOLS,
                },
                {
                    "id": "haku_v1",
                    "type": "any_of",
                    "policies": ["safe_tools", "managed_gmail_labels", "public_ducktape_reads"],
                },
                {"id": "none", "type": "never"},
            ],
            "static_agents": [
                {
                    "agent_id": str(AGENT_ACTOR.agent_id),
                    "display_name": "Test Agent",
                    "token_env_var": "TEST_AGENT_TOKEN",
                    "operator_subject_env": "TEST_AGENT_OPERATOR",
                    "auto_approval_policy": "haku_v1",
                }
            ],
        }
    )
)


async def _decision(tool_name: str, arguments: dict, *, gmail=None, actor: ToolCallActor = AGENT_ACTOR):
    gmail = gmail or Mock()
    return await auto_approve_tool_call(
        policies=_POLICIES,
        actor=actor,
        server_id="gmail",
        tool_name=tool_name,
        arguments=arguments,
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
        policies=_POLICIES,
        actor=AGENT_ACTOR,
        server_id="google_calendar",
        tool_name=tool_name,
        arguments=arguments,
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
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "exact tool" in evaluation


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
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "exact tool" in evaluation


async def test_calendar_create_stays_manual() -> None:
    policy_id, evaluation = _approval(
        await _calendar_decision(
            "create_event", {"summary": "Standup", "start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}
        )
    )
    assert policy_id is None
    assert evaluation == "manual: Agent policy 'haku_v1' did not auto-approve google_calendar/create_event"


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


def test_policy_graph_reports_clear_tool_modes() -> None:
    assert _POLICIES.tool_mode(AGENT_ACTOR, "gmail", "labels_list") is ToolAutoApprovalMode.ALWAYS_AUTO_APPROVED
    assert (
        _POLICIES.tool_mode(AGENT_ACTOR, "gmail", "labels_delete") is ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
    )
    assert _POLICIES.tool_mode(AGENT_ACTOR, "gmail", "drafts_create") is ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED


async def test_unassigned_agent_fails_closed_to_manual_approval() -> None:
    unassigned = AgentActor(
        agent_id=UUID("00000000-0000-0000-0000-000000000099"),
        operator_id=TEST_OPERATOR_ID,
        binding_id=UUID("00000000-0000-0000-0000-000000000098"),
    )
    decision = await _decision("labels_list", {}, actor=unassigned)
    assert decision == (None, "manual: Agent has no auto-approval policy for gmail/labels_list")


async def test_durable_actor_policy_auto_approves_without_a_static_config_assignment() -> None:
    enrolled = AgentActor(
        agent_id=UUID("00000000-0000-0000-0000-000000000099"),
        operator_id=TEST_OPERATOR_ID,
        binding_id=UUID("00000000-0000-0000-0000-000000000098"),
        auto_approval_policy="haku_v1",
    )
    policy_id, evaluation = await _decision("labels_list", {}, actor=enrolled)
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "haku_v1" in evaluation


async def test_durable_actor_policy_overrides_the_static_rollout_fallback() -> None:
    manually_approved = AgentActor(
        agent_id=AGENT_ACTOR.agent_id,
        operator_id=AGENT_ACTOR.operator_id,
        binding_id=AGENT_ACTOR.binding_id,
        auto_approval_policy="none",
    )
    assert await _decision("labels_list", {}, actor=manually_approved) == (
        None,
        "manual: Agent policy 'none' did not auto-approve gmail/labels_list (none: policy never auto-approves)",
    )


def test_policy_config_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="contains a cycle"):
        ConsoleConfigFile.model_validate(
            {
                "auto_approval_policies": [
                    {"id": "one", "type": "any_of", "policies": ["two"]},
                    {"id": "two", "type": "any_of", "policies": ["one"]},
                ]
            }
        )


def test_policy_config_rejects_unknown_agent_policy() -> None:
    with pytest.raises(ValidationError, match="unknown auto-approval policy"):
        ConsoleConfigFile.model_validate(
            {
                "static_agents": [
                    {
                        "agent_id": str(AGENT_ACTOR.agent_id),
                        "display_name": "Test Agent",
                        "token_env_var": "TEST_AGENT_TOKEN",
                        "operator_subject_env": "TEST_AGENT_OPERATOR",
                        "auto_approval_policy": "missing",
                    }
                ]
            }
        )


def test_static_agent_policy_assignment_is_required() -> None:
    with pytest.raises(ValidationError, match="auto_approval_policy"):
        ConsoleConfigFile.model_validate(
            {
                "static_agents": [
                    {
                        "agent_id": str(AGENT_ACTOR.agent_id),
                        "display_name": "Test Agent",
                        "token_env_var": "TEST_AGENT_TOKEN",
                        "operator_subject_env": "TEST_AGENT_OPERATOR",
                    }
                ]
            }
        )


def test_fail_closed_default_is_selected_by_policy_type_not_magic_id() -> None:
    config = ConsoleConfigFile.model_validate({"auto_approval_policies": [{"id": "operator_review", "type": "never"}]})

    assert config.default_agent_auto_approval_policy == "operator_review"


async def _remote_decision(server_id: str, tool_name: str, arguments: dict) -> tuple[str | None, str | None]:
    # Remote (operator_oauth) servers have no in-process schema, so `mcp` is None.
    return _approval(
        await auto_approve_tool_call(
            policies=_POLICIES,
            actor=AGENT_ACTOR,
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
            gmail=None,
            mcp=None,
        )
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("issue_read", {"owner": "agentydragon", "repo": "ducktape", "issue_number": 123}),
        ("pull_request_read", {"owner": "agentydragon", "repo": "ducktape", "pullNumber": 456, "method": "get"}),
        ("actions_list", {"owner": "agentydragon", "repo": "ducktape", "method": "list_workflow_runs"}),
        ("get_job_logs", {"owner": "agentydragon", "repo": "ducktape", "run_id": 789, "failed_only": True}),
    ],
)
async def test_public_ducktape_reads_auto_approve(tool_name: str, arguments: dict) -> None:
    policy_id, evaluation = await _remote_decision("github", tool_name, arguments)
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "reviewed read targets repository agentydragon/ducktape" in evaluation


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("issue_read", {"owner": "agentydragon", "repo": "private", "issue_number": 123}),
        ("get_job_logs", {"owner": "someone", "repo": "ducktape", "run_id": 789}),
        ("get_file_contents", {"owner": "agentydragon", "path": "README.md"}),
        ("search_code", {"query": "repo:agentydragon/ducktape policy"}),
    ],
)
async def test_other_or_unprovable_github_reads_stay_manual(tool_name: str, arguments: dict) -> None:
    policy_id, evaluation = await _remote_decision("github", tool_name, arguments)
    assert policy_id is None
    assert evaluation is not None


async def test_github_write_stays_manual() -> None:
    assert await _remote_decision(
        "github", "create_issue", {"owner": "agentydragon", "repo": "ducktape", "title": "No"}
    ) == (None, "manual: Agent policy 'haku_v1' did not auto-approve github/create_issue")


async def test_grocy_reads_auto_approve() -> None:
    policy_id, evaluation = await _remote_decision("grocy-sf", "products_list", {"detail": "brief"})
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "exact tool" in evaluation


async def test_grocy_writes_stay_manual() -> None:
    assert await _remote_decision("grocy-sf", "products_create", {"name": "Milk"}) == (
        None,
        "manual: Agent policy 'haku_v1' did not auto-approve grocy-sf/products_create",
    )


async def test_lookup_errors_are_logged_and_fail_closed(caplog: pytest.LogCaptureFixture) -> None:
    gmail = Mock()
    gmail.labels_get.side_effect = RuntimeError("gmail unavailable")
    with caplog.at_level("ERROR"):
        policy_id, evaluation = await _decision("labels_delete", {"label_id": "Label_1"}, gmail=gmail)
        assert policy_id is None
        assert evaluation is not None
        assert "Gmail auto-approval evaluation failed" in evaluation
    assert "auto-approval evaluation failed" in caplog.text
    assert "gmail unavailable" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
