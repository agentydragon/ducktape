"""Tests for the auto-approval policy graph: registry composition and each policy kind's outcomes.

The config here spans every policy kind on purpose -- several tests (e.g. the multi-actor GitHub
searches) exist specifically to verify a policy composes correctly through `any_of` from more than
one access profile, which a per-evaluator unit test wouldn't cover."""

from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError

from gmail_api.labels import GmailLabel, LabelType
from haku.console.auto_approval.github import GitHubRepositoryVisibilityService
from haku.console.auto_approval.registry import (
    AGENT_AUTO_APPROVAL_ID,
    AutoApprovalPolicyRegistry,
    PolicyDenial,
    ToolAutoApprovalMode,
    auto_approve_tool_call,
)
from haku.console.mcp_config import AccessProfile, ConsoleConfigFile
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tools.gmail import build_mcp
from haku.console.tools.google_calendar import build_mcp as build_calendar_mcp

TEST_OPERATOR_ID = UUID("00000000-0000-0000-0000-000000000001")
AGENT_ACTOR = AgentActor(
    agent_id=UUID("00000000-0000-0000-0000-000000000002"),
    operator_id=TEST_OPERATOR_ID,
    binding_id=UUID("00000000-0000-0000-0000-000000000003"),
    access_profile_id="haku",
)
PUBLIC_CODER_ACTOR = AgentActor(
    agent_id=UUID("00000000-0000-0000-0000-000000000004"),
    operator_id=TEST_OPERATOR_ID,
    binding_id=UUID("00000000-0000-0000-0000-000000000005"),
    access_profile_id="public-coder",
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
_GITHUB_TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "actions_get": {"method": "list_workflow_runs"},
    "actions_list": {"method": "list_workflow_runs"},
    "get_file_contents": {"path": "README.md"},
    "get_job_logs": {"run_id": 789, "failed_only": True},
    "issue_read": {"issue_number": 123},
    "list_issues": {},
    "list_pull_requests": {},
    "pull_request_read": {"pullNumber": 456, "method": "get"},
    "search_pull_requests": {"query": "is:open"},
}
_GITHUB_TOOLS = [*_GITHUB_TOOL_ARGUMENTS, "search_code"]
_MANUAL_AUTHORITY_CONFIG = {
    "auto_approval_policies": [{"id": "manual", "type": "never"}],
    "access_profiles": [{"id": "manual", "auto_approval_policy": "manual"}],
    "default_access_profile_id": "manual",
}
_CONFIG = ConsoleConfigFile.model_validate(
    {
        "mcp": {"servers": _SERVER_CONFIGS},
        "auto_approval_policies": [
            {"id": "safe_tools", "type": "exact_tools", "tools": _EXACT_TOOLS},
            {"id": "managed_gmail_labels", "type": "gmail_label_namespace", "server": "gmail", "label_prefix": "haku/"},
            {
                "id": "public_ducktape_reads",
                "type": "github_repository",
                "server": "github",
                "owner": "agentydragon",
                "repository": "ducktape",
                "tools": _GITHUB_TOOLS,
            },
            {
                "id": "public_gaffer_private_reads",
                "type": "github_repository",
                "server": "github",
                "owner": "agentydragon",
                "repository": "gaffer-private",
                "tools": _GITHUB_TOOLS,
            },
            {
                "id": "haku_v1",
                "type": "any_of",
                "policies": [
                    "safe_tools",
                    "managed_gmail_labels",
                    "public_ducktape_reads",
                    "public_gaffer_private_reads",
                ],
            },
            {
                "id": "public_github_reads",
                "type": "github_public_repository",
                "server": "github",
                "tools": _GITHUB_TOOLS,
            },
            {
                "id": "public_coder_github_reads",
                "type": "any_of",
                "policies": ["public_ducktape_reads", "public_gaffer_private_reads", "public_github_reads"],
            },
            {"id": "none", "type": "never"},
        ],
        "access_profiles": [
            {"id": "haku", "auto_approval_policy": "haku_v1"},
            {"id": "public-coder", "auto_approval_policy": "public_coder_github_reads"},
            {"id": "manual", "auto_approval_policy": "none"},
        ],
        "default_access_profile_id": "manual",
        "static_agents": [
            {
                "agent_id": str(AGENT_ACTOR.agent_id),
                "display_name": "Test Agent",
                "token_env_var": "TEST_AGENT_TOKEN",
                "operator_subject_env": "TEST_AGENT_OPERATOR",
                "access_profile_id": "haku",
            }
        ],
    }
)
_POLICIES = AutoApprovalPolicyRegistry(_CONFIG)


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


def _approval(decision: tuple[str | None, str | None] | PolicyDenial) -> tuple[str | None, str | None]:
    """Unwrap a decision the test expects NOT to be a terminal schema denial."""
    assert not isinstance(decision, PolicyDenial), decision
    return decision


async def _policy_id(tool_name: str, arguments: dict, **kwargs):
    policy_id, _evaluation = _approval(await _decision(tool_name, arguments, **kwargs))
    return policy_id


async def _calendar_decision(tool_name: str, arguments: dict) -> tuple[str | None, str | None] | PolicyDenial:
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
    assert isinstance(denial, PolicyDenial)
    assert denial.evaluation == "denied: arguments failed the registered tool schema"
    assert "251" in denial.reason  # the concrete validation error reaches the caller


async def test_read_with_unknown_argument_is_auto_denied() -> None:
    denial = await _decision("threads_list", {"q": "", "unexpected": True})
    assert isinstance(denial, PolicyDenial)
    assert "unexpected" in denial.reason


@pytest.mark.parametrize("field", ["add", "remove"])
async def test_modifies_only_namespaced_labels(field: str) -> None:
    assert await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], field: ["haku/triaged"]})
    assert await _policy_id("threads_modify_labels", {"thread_ids": ["t1"], field: ["INBOX"]}) is None


async def test_modify_rejects_unknown_arguments() -> None:
    denial = await _decision(
        "threads_modify_labels", {"thread_ids": ["t1"], "add": ["haku/triaged"], "unexpected": True}
    )
    assert isinstance(denial, PolicyDenial)
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
    assert decision == (None, "manual: Agent has no configured access profile for gmail/labels_list")


async def test_durable_actor_profile_auto_approves_without_a_static_config_assignment() -> None:
    enrolled = AgentActor(
        agent_id=UUID("00000000-0000-0000-0000-000000000099"),
        operator_id=TEST_OPERATOR_ID,
        binding_id=UUID("00000000-0000-0000-0000-000000000098"),
        access_profile_id="haku",
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
        access_profile_id="manual",
    )
    assert await _decision("labels_list", {}, actor=manually_approved) == (
        None,
        "manual: Agent policy 'none' did not auto-approve gmail/labels_list (none: policy never auto-approves)",
    )


def test_policy_config_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="contains a cycle"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "auto_approval_policies": [
                    {"id": "one", "type": "any_of", "policies": ["two"]},
                    {"id": "two", "type": "any_of", "policies": ["one"]},
                    {"id": "manual", "type": "never"},
                ],
            }
        )


def test_profile_config_rejects_unknown_static_agent_profile() -> None:
    with pytest.raises(ValidationError, match="unknown access profile"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "static_agents": [
                    {
                        "agent_id": str(AGENT_ACTOR.agent_id),
                        "display_name": "Test Agent",
                        "token_env_var": "TEST_AGENT_TOKEN",
                        "operator_subject_env": "TEST_AGENT_OPERATOR",
                        "access_profile_id": "missing",
                    }
                ],
            }
        )


def test_profile_config_rejects_unknown_kubernetes_authorization_profile() -> None:
    with pytest.raises(ValidationError, match="Kubernetes authorization references unknown access profiles"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "kubernetes_authorization": {
                    "subjects_by_access_profile": {"missing": {"username": "system:serviceaccount:ns:reader"}}
                },
            }
        )


def test_kubernetes_server_requires_authorization_configuration() -> None:
    with pytest.raises(ValidationError, match="requires Kubernetes authorization configuration"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "mcp": {
                    "servers": [{"id": "kubernetes", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}]
                },
            }
        )


def test_static_agent_access_profile_assignment_is_required() -> None:
    with pytest.raises(ValidationError, match="access_profile_id"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "static_agents": [
                    {
                        "agent_id": str(AGENT_ACTOR.agent_id),
                        "display_name": "Test Agent",
                        "token_env_var": "TEST_AGENT_TOKEN",
                        "operator_subject_env": "TEST_AGENT_OPERATOR",
                    }
                ],
            }
        )


def test_default_access_profile_does_not_require_a_never_policy() -> None:
    config = ConsoleConfigFile.model_validate(
        {
            "auto_approval_policies": [
                {"id": "operator_review", "type": "never"},
                {"id": "selected_by_default", "type": "any_of", "policies": ["operator_review"]},
            ],
            "access_profiles": [{"id": "operator-default", "auto_approval_policy": "selected_by_default"}],
            "default_access_profile_id": "operator-default",
        }
    )

    assert config.default_access_profile_id == "operator-default"


def test_profile_config_rejects_unknown_recall_index() -> None:
    with pytest.raises(ValidationError, match="unknown Recall indexes"):
        ConsoleConfigFile.model_validate(
            {
                "auto_approval_policies": [{"id": "operator_review", "type": "never"}],
                "access_profiles": [
                    {
                        "id": "operator-review",
                        "auto_approval_policy": "operator_review",
                        "recall_index_ids": ["not-configured"],
                    }
                ],
                "default_access_profile_id": "operator-review",
            }
        )


def test_access_profile_recall_index_ids_are_a_set() -> None:
    profile = AccessProfile.model_validate(
        {
            "id": "operator-review",
            "auto_approval_policy": "operator_review",
            "recall_index_ids": ["ducktape-public", "ducktape-public"],
        }
    )

    assert profile.recall_index_ids == {"ducktape-public"}


async def _remote_decision(
    server_id: str, tool_name: str, arguments: dict, *, actor: ToolCallActor = AGENT_ACTOR
) -> tuple[str | None, str | None]:
    # Remote (operator_oauth) servers have no in-process schema, so `mcp` is None.
    return _approval(
        await auto_approve_tool_call(
            policies=_POLICIES,
            actor=actor,
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


@pytest.mark.parametrize(("tool_name", "tool_arguments"), list(_GITHUB_TOOL_ARGUMENTS.items()))
async def test_private_gaffer_reads_auto_approve(tool_name: str, tool_arguments: dict[str, object]) -> None:
    arguments = {"owner": "agentydragon", "repo": "gaffer-private", **tool_arguments}
    policy_id, evaluation = await _remote_decision("github", tool_name, arguments)
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "reviewed read targets repository agentydragon/gaffer-private" in evaluation


@pytest.mark.parametrize("actor", [AGENT_ACTOR, PUBLIC_CODER_ACTOR], ids=["haku", "public-coder"])
@pytest.mark.parametrize("repository", ["ducktape", "gaffer-private"])
async def test_approved_agents_can_search_pull_requests_in_reviewed_repositories(
    actor: AgentActor, repository: str
) -> None:
    policy_id, evaluation = await _remote_decision(
        "github",
        "search_pull_requests",
        {"owner": "agentydragon", "repo": repository, "query": "is:open author:agentydragon-agent"},
        actor=actor,
    )
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert f"reviewed read targets repository agentydragon/{repository}" in evaluation


@pytest.mark.parametrize("actor", [AGENT_ACTOR, PUBLIC_CODER_ACTOR], ids=["haku", "public-coder"])
@pytest.mark.parametrize("repository", ["ducktape", "gaffer-private"])
async def test_approved_agents_can_search_code_in_reviewed_repositories(actor: AgentActor, repository: str) -> None:
    policy_id, evaluation = await _remote_decision(
        "github", "search_code", {"query": f"repo:agentydragon/{repository} language:python authorization"}, actor=actor
    )
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert f"reviewed code search targets repository agentydragon/{repository}" in evaluation


@pytest.mark.parametrize(
    "query", ["repo:agentydragon/other is:open", "-repo:agentydragon/other is:open", "Repo:agentydragon/other is:open"]
)
async def test_public_coder_pr_search_with_repository_qualifier_stays_manual(query: str) -> None:
    policy_id, evaluation = await _remote_decision(
        "github",
        "search_pull_requests",
        {"owner": "agentydragon", "repo": "ducktape", "query": query},
        actor=PUBLIC_CODER_ACTOR,
    )
    assert policy_id is None
    assert evaluation is not None
    assert "repository qualifier" in evaluation


@pytest.mark.parametrize(
    "query",
    [
        "authorization",
        "repo:agentydragon/other authorization",
        "repo:agentydragon/ducktape repo:agentydragon/other authorization",
        '"repo:agentydragon/ducktape" authorization',
        "-repo:agentydragon/ducktape authorization",
    ],
)
async def test_public_coder_code_search_without_exact_repository_scope_stays_manual(query: str) -> None:
    policy_id, evaluation = await _remote_decision("github", "search_code", {"query": query}, actor=PUBLIC_CODER_ACTOR)
    assert policy_id is None
    assert evaluation is not None
    assert "code search" in evaluation or "repository agentydragon/other is outside" in evaluation


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("issue_read", {"owner": "agentydragon", "repo": "private", "issue_number": 123}),
        ("get_job_logs", {"owner": "someone", "repo": "ducktape", "run_id": 789}),
        ("get_file_contents", {"owner": "agentydragon", "path": "README.md"}),
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


def _github_visibility_handler(*public_repositories: tuple[str, str], unavailable: bool = False):
    """A MockTransport handler standing in for GitHub's unauthenticated repo-visibility endpoint."""
    confirmed_public = {(owner.casefold(), repo.casefold()) for owner, repo in public_repositories}

    def handle(request: httpx.Request) -> httpx.Response:
        if unavailable:
            return httpx.Response(500)
        _, _, owner, repository = request.url.path.split("/", 3)
        if (owner.casefold(), repository.casefold()) in confirmed_public:
            return httpx.Response(200, json={"private": False})
        return httpx.Response(404)

    return handle


def _policies_with_visibility(handler) -> AutoApprovalPolicyRegistry:
    http_client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return AutoApprovalPolicyRegistry(
        _CONFIG, github_repository_visibility=GitHubRepositoryVisibilityService(http_client, ttl_seconds=3600.0)
    )


async def _public_repo_decision(
    tool_name: str, arguments: dict, *, handler, actor: ToolCallActor = PUBLIC_CODER_ACTOR
) -> tuple[str | None, str | None]:
    return _approval(
        await auto_approve_tool_call(
            policies=_policies_with_visibility(handler),
            actor=actor,
            server_id="github",
            tool_name=tool_name,
            arguments=arguments,
            gmail=None,
            mcp=None,
        )
    )


async def test_confirmed_public_third_party_repo_auto_approves() -> None:
    handler = _github_visibility_handler(("redpanda-data", "ducktape"))

    policy_id, evaluation = await _public_repo_decision(
        "get_file_contents", {"owner": "redpanda-data", "repo": "ducktape", "path": "README.md"}, handler=handler
    )

    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "confirmed-public repository redpanda-data/ducktape" in evaluation


async def test_unconfirmed_repo_stays_manual() -> None:
    handler = _github_visibility_handler()  # nothing is confirmed public

    policy_id, evaluation = await _public_repo_decision(
        "issue_read", {"owner": "someone", "repo": "private-thing", "issue_number": 1}, handler=handler
    )

    assert policy_id is None
    assert evaluation is not None
    assert "not confirmed public" in evaluation


async def test_visibility_check_failure_stays_manual() -> None:
    """A GitHub-side outage must fail closed, never silently approve."""
    handler = _github_visibility_handler(("redpanda-data", "ducktape"), unavailable=True)

    policy_id, evaluation = await _public_repo_decision(
        "get_file_contents", {"owner": "redpanda-data", "repo": "ducktape", "path": "README.md"}, handler=handler
    )

    assert policy_id is None
    assert evaluation is not None
    assert "could not confirm" in evaluation


async def test_public_repo_code_search_confirms_the_qualifier_repository() -> None:
    handler = _github_visibility_handler(("redpanda-data", "ducktape"))

    policy_id, evaluation = await _public_repo_decision(
        "search_code", {"query": "repo:redpanda-data/ducktape language:python"}, handler=handler
    )

    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "confirmed-public repository redpanda-data/ducktape" in evaluation


async def test_public_repo_pull_request_search_still_rejects_a_smuggled_qualifier() -> None:
    """The same anti-smuggling boundary as the fixed-repo policies: owner/repo names a confirmed-
    public repository, but the query's own repo: qualifier would actually target a different one."""
    handler = _github_visibility_handler(("redpanda-data", "ducktape"))

    policy_id, evaluation = await _public_repo_decision(
        "search_pull_requests",
        {"owner": "redpanda-data", "repo": "ducktape", "query": "repo:someone/private-thing is:open"},
        handler=handler,
    )

    assert policy_id is None
    assert evaluation is not None
    assert "repository qualifier" in evaluation


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
