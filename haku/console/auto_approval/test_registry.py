"""Tests for the auto-approval policy graph: registry composition and each policy kind's outcomes.

Some tests (e.g. `revoke_grants` under both Agent roots) exist specifically to verify a policy
composes correctly through `any_of` from more than one access profile, which a per-evaluator unit
test wouldn't cover.

gmail/google_calendar are no longer in-process haku-console servers (see
`x/google_mcp_server`), but their real MCP tool schemas are still reachable there and make a
realistic example of a server with several tools and non-trivial argument schemas — reused here
purely to exercise the generic exact-tools/schema-validation machinery, not any gmail-specific
policy (the gmail label-namespace auto-approval policy this file used to also cover was removed
along with the in-process server)."""

from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.auto_approval.registry import (
    AGENT_AUTO_APPROVAL_ID,
    AutoApprovalPolicyRegistry,
    PolicyDenial,
    ToolAutoApprovalMode,
    auto_approve_tool_call,
)
from haku.console.mcp_config import AccessProfile, ConsoleConfigFile
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from x.google_mcp_server.gmail import build_mcp
from x.google_mcp_server.google_calendar import build_mcp as build_calendar_mcp

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
_SERVER_CONFIGS: dict[str, dict[str, Any]] = {
    server_id.replace("-", "_"): {"id": server_id, "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
    for server_id in _EXACT_TOOLS
}
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
            {"id": "haku_v1", "type": "any_of", "policies": ["safe_tools"]},
            {"id": "none", "type": "never"},
        ],
        "access_profiles": [
            {"id": "haku", "auto_approval_policy": "haku_v1"},
            {"id": "manual", "auto_approval_policy": "none"},
        ],
        "default_access_profile_id": "manual",
        "static_agents": {
            "test": {
                "agent_id": str(AGENT_ACTOR.agent_id),
                "display_name": "Test Agent",
                "token": "test-agent-token",
                "operator_subject": "test-agent-operator",
                "access_profile_id": "haku",
            }
        },
    }
)
_POLICIES = AutoApprovalPolicyRegistry(_CONFIG)


async def _decision(tool_name: str, arguments: dict, *, actor: RuntimeActor = AGENT_ACTOR):
    return await auto_approve_tool_call(
        policies=_POLICIES,
        actor=actor,
        server_id="gmail",
        tool_name=tool_name,
        arguments=arguments,
        mcp=build_mcp(Mock()),
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


async def test_operator_actor_is_not_auto_approved() -> None:
    assert await _decision("labels_list", {}, actor=OPERATOR_ACTOR) == (None, None)


def test_policy_graph_reports_clear_tool_modes() -> None:
    assert _POLICIES.tool_mode(AGENT_ACTOR, "gmail", "labels_list") is ToolAutoApprovalMode.ALWAYS_AUTO_APPROVED
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
                "static_agents": {
                    "test": {
                        "agent_id": str(AGENT_ACTOR.agent_id),
                        "display_name": "Test Agent",
                        "token": "test-agent-token",
                        "operator_subject": "test-agent-operator",
                        "access_profile_id": "missing",
                    }
                },
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
                    "servers": {
                        "kubernetes": {
                            "id": "kubernetes",
                            "backend": {"kind": "in_process", "credential": {"kind": "none"}},
                        }
                    }
                },
            }
        )


def test_static_agent_access_profile_assignment_is_required() -> None:
    with pytest.raises(ValidationError, match="access_profile_id"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "static_agents": {
                    "test": {
                        "agent_id": str(AGENT_ACTOR.agent_id),
                        "display_name": "Test Agent",
                        "token": "test-agent-token",
                        "operator_subject": "test-agent-operator",
                    }
                },
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


async def _schemaless_decision(
    server_id: str, tool_name: str, arguments: dict, *, actor: RuntimeActor = AGENT_ACTOR
) -> tuple[str | None, str | None]:
    # No registered server builder, so no schema to validate against: `mcp` is None.
    return _approval(
        await auto_approve_tool_call(
            policies=_POLICIES, actor=actor, server_id=server_id, tool_name=tool_name, arguments=arguments, mcp=None
        )
    )


async def test_grocy_reads_auto_approve() -> None:
    policy_id, evaluation = await _schemaless_decision("grocy-sf", "products_list", {"detail": "brief"})
    assert policy_id == AGENT_AUTO_APPROVAL_ID
    assert evaluation is not None
    assert "exact tool" in evaluation


async def test_grocy_writes_stay_manual() -> None:
    assert await _schemaless_decision("grocy-sf", "products_create", {"name": "Milk"}) == (
        None,
        "manual: Agent policy 'haku_v1' did not auto-approve grocy-sf/products_create",
    )


# A registry whose only policy is the argument-conditional own-grant list read (#4918).
_GRANT_READS_REGISTRY = AutoApprovalPolicyRegistry(
    ConsoleConfigFile.model_validate(
        {
            "mcp": {
                "servers": {
                    "grants": {"id": "grants", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
                }
            },
            "auto_approval_policies": [{"id": "grants_own_list", "type": "grant_self_list", "server": "grants"}],
            "access_profiles": [{"id": "haku", "auto_approval_policy": "grants_own_list"}],
            "default_access_profile_id": "haku",
            "static_agents": {
                "test": {
                    "agent_id": str(AGENT_ACTOR.agent_id),
                    "display_name": "Test Agent",
                    "token": "test-agent-token",
                    "operator_subject": "test-agent-operator",
                    "access_profile_id": "haku",
                }
            },
        }
    )
)


def test_grant_self_list_is_conditional_only_for_list_grants() -> None:
    assert (
        _GRANT_READS_REGISTRY.tool_mode(AGENT_ACTOR, "grants", "list_grants")
        is ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
    )
    # No other grant verb — including the unconditional reads served by a separate exact-tools policy
    # in production — is auto-approvable through this argument-conditional one.
    for tool in ("get_grant", "create_grant", "revoke_grants", "kubernetes_can_i"):
        assert (
            _GRANT_READS_REGISTRY.tool_mode(AGENT_ACTOR, "grants", tool)
            is ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
        )


async def test_list_grants_auto_approves_only_the_explicit_self_scope() -> None:
    approved = await _GRANT_READS_REGISTRY.evaluate(
        actor=AGENT_ACTOR, server_id="grants", tool_name="list_grants", arguments={"principal": "self"}
    )
    assert not isinstance(approved, PolicyDenial)
    assert approved[0] == AGENT_AUTO_APPROVAL_ID
    approved_with_history = await _GRANT_READS_REGISTRY.evaluate(
        actor=AGENT_ACTOR,
        server_id="grants",
        tool_name="list_grants",
        arguments={"principal": "self", "include_inactive": True},
    )
    assert not isinstance(approved_with_history, PolicyDenial)
    assert approved_with_history[0] == AGENT_AUTO_APPROVAL_ID
    # Omission and a named principal stay manual.
    for arguments in ({}, {"principal": None}, {"principal": {"kind": "agent", "agent_id": str(AGENT_ACTOR.agent_id)}}):
        manual = await _GRANT_READS_REGISTRY.evaluate(
            actor=AGENT_ACTOR, server_id="grants", tool_name="list_grants", arguments=arguments
        )
        assert not isinstance(manual, PolicyDenial)
        assert manual[0] is None


# A registry mirroring production's grants_own_revoke composition: the exact-tools own-revoke atom
# reached through each agent profile's root any_of. revoke_grants is narrowing (an Agent relinquishes
# only its OWN grants — owner_agent_id is operator-only and rejected for an Agent) and click-free;
# create_grant is widening (it mints new temporary authority) and stays manual.
_OWN_REVOKE_REGISTRY = AutoApprovalPolicyRegistry(
    ConsoleConfigFile.model_validate(
        {
            "mcp": {
                "servers": {
                    "grants": {"id": "grants", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
                }
            },
            "auto_approval_policies": [
                {"id": "grants_own_revoke", "type": "exact_tools", "tools": {"grants": ["revoke_grants"]}},
                {"id": "haku_v1", "type": "any_of", "policies": ["grants_own_revoke"]},
                {"id": "public_coder_safe_reads", "type": "any_of", "policies": ["grants_own_revoke"]},
            ],
            "access_profiles": [
                {"id": "haku", "auto_approval_policy": "haku_v1"},
                {"id": "public-coder", "auto_approval_policy": "public_coder_safe_reads"},
            ],
            "default_access_profile_id": "haku",
        }
    )
)


@pytest.mark.parametrize("actor", [AGENT_ACTOR, PUBLIC_CODER_ACTOR], ids=["haku", "public-coder"])
def test_revoke_grants_is_click_free_under_both_agent_roots(actor: AgentActor) -> None:
    # Composed into both agent roots (haku_v1 and public_coder_safe_reads), an Agent's own-relinquish
    # revoke is unconditionally auto-approved.
    assert _OWN_REVOKE_REGISTRY.tool_mode(actor, "grants", "revoke_grants") is ToolAutoApprovalMode.ALWAYS_AUTO_APPROVED
    # create_grant (widening) is never listed, so it stays manual under the same roots.
    assert (
        _OWN_REVOKE_REGISTRY.tool_mode(actor, "grants", "create_grant") is ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
    )


if __name__ == "__main__":
    pytest_bazel.main()
