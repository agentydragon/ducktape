"""How a caller's bindings resolve from the index at one instant, what the caller and the operator
read of that resolution, and the provider deciding the GitHub repository kinds and the Home
Assistant kind over it: a match carries its policy in evidence, every miss is no opinion."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.models import (
    MatchedPolicy,
    MatchedRepository,
    PolicyKind,
    PolicySource,
    ProviderVerdict,
    service_account_key,
)
from agentplane.action_service.policy_evaluation import AUTO_APPROVE_REASON, PolicySetDecisionProvider, resolve_bindings
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.policy_view import (
    ArgumentSchemaView,
    ExactActionsView,
    GitHubPublicRepositoryView,
    GitHubRepositoryView,
    HomeAssistantEntityControlView,
    caller_view,
    subject_view,
)
from agentplane.action_service.providers import BindingKind, DecisionContext
from agentplane.action_service.testing.policies import add_policy_binding, add_policy_set, add_rule_set
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef

NAMESPACE = "agentplane-test"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
WORKLOAD = ServiceAccountRef(namespace=NAMESPACE, name="workload-1")
GRANTED = ServiceAccountRef(namespace=NAMESPACE, name="test-caller")
CALLER = GRANTED
SCHEMA_POLICY: dict[str, Any] = {
    "type": "argument_schema",
    "actions": {"everything": ["echo"]},
    "schema": {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string", "maxLength": 5}, "count": {"type": "integer"}},
        "additionalProperties": False,
    },
}


type SetSpec = tuple[str, list[dict[str, Any]]]


def index_of(*, synced: bool = True) -> PolicyIndex:
    return PolicyIndex(
        listed=synced, freshness=Freshness(stale_after_seconds=900, at={"test-kind": NOW}), clock=lambda: NOW
    )


def test_resolution_takes_the_bindings_naming_the_caller_with_their_existing_valid_sets() -> None:
    index = index_of()
    add_policy_set(index, "set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    add_policy_set(index, "set-broken", [{"type": "nope", "actions": {"everything": ["echo"]}}])
    add_policy_binding(index, "b-workload", WORKLOAD, ["set-reads", "set-broken", "set-missing"])
    add_policy_binding(index, "b-granted", GRANTED, ["set-reads"])
    add_policy_binding(index, "b-later", WORKLOAD, ["set-reads"], expires_at=NOW + timedelta(hours=1))
    add_policy_binding(
        index, "b-other-account", ServiceAccountRef(namespace=NAMESPACE, name="someone-else"), ["set-reads"]
    )
    for_workload = resolve_bindings(index, WORKLOAD)
    assert [resolved.name for resolved in for_workload] == ["b-later", "b-workload"]
    assert [[s.name for s in resolved.policy_sets] for resolved in for_workload] == [["set-reads"], ["set-reads"]]
    for_account = resolve_bindings(index, CALLER)
    assert [resolved.name for resolved in for_account] == ["b-granted"]
    assert resolve_bindings(index, ServiceAccountRef(namespace="agentplane-other", name=WORKLOAD.name)) == ()
    assert resolve_bindings(index, ServiceAccountRef(namespace=NAMESPACE, name="x")) == ()


def test_nothing_resolves_before_the_informer_has_synced() -> None:
    index = index_of(synced=False)
    add_policy_set(index, "set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    add_policy_binding(index, "b-workload", WORKLOAD, ["set-reads"])
    assert resolve_bindings(index, WORKLOAD) == ()
    index.listed = True
    assert len(resolve_bindings(index, WORKLOAD)) == 1


def test_every_kind_projects_to_its_own_view() -> None:
    index = index_of()
    add_policy_set(
        index,
        "set-kinds",
        [
            {"type": "exact_actions", "actions": {"everything": ["echo"]}},
            SCHEMA_POLICY,
            {"type": "github_repository", "actions": {"github": ["search_code"]}, "owner": "o", "repository": "r"},
            {"type": "github_public_repository", "actions": {"github": ["get_file_contents"]}},
            {
                "type": "home_assistant_entity_control",
                "actions": {"ha-test": ["ha_call_service"]},
                "entities": {"light.test_lamp": ["turn_on", "toggle"], "fan.test_fan": ["turn_off"]},
            },
        ],
    )
    add_policy_binding(index, "b-kinds", WORKLOAD, ["set-kinds"])
    views = [entry.policy for entry in caller_view(index, WORKLOAD).auto_approve_if]
    assert [view.type for view in views] == list(PolicyKind)
    assert isinstance(views[2], GitHubRepositoryView)
    assert (views[2].owner, views[2].repository, views[2].actions) == ("o", "r", {"github": ["search_code"]})
    assert isinstance(views[3], GitHubPublicRepositoryView)
    assert views[3].actions == {"github": ["get_file_contents"]}
    assert isinstance(views[4], HomeAssistantEntityControlView)
    assert views[4].entities == {"fan.test_fan": ["turn_off"], "light.test_lamp": ["toggle", "turn_on"]}


@pytest.fixture
def mixed_index() -> PolicyIndex:
    """One ServiceAccount with a valid, a refused and a missing set on one binding, a second binding
    of its own, and the bindings the resolution skips: another account's, and one held through a
    Connection grant."""
    index = index_of()
    add_policy_set(index, "set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo", "add"]}}])
    add_policy_set(index, "set-echo", [SCHEMA_POLICY], generation=3)
    add_policy_set(index, "set-broken", [{"type": "nope", "actions": {"everything": ["echo"]}}])
    add_policy_binding(
        index,
        "b-workload",
        WORKLOAD,
        ["set-reads", "set-broken", "set-missing"],
        labels={"test.example/managed-by": "test-writer"},
    )
    add_policy_binding(index, "b-afternoon", WORKLOAD, ["set-echo"], expires_at=NOW + timedelta(hours=1))
    add_policy_binding(
        index, "b-other-account", ServiceAccountRef(namespace=NAMESPACE, name="someone-else"), ["set-reads"]
    )
    add_policy_binding(index, "b-granted", GRANTED, ["set-echo"])
    return index


def test_a_caller_reads_its_own_resolution_and_nothing_of_the_sets_it_cannot_use(mixed_index: PolicyIndex) -> None:
    """The caller view names the bindings admission resolves and, per binding, only the sets that
    contribute: a refused or missing set is simply not there, and another subject's bindings never are."""
    view = caller_view(mixed_index, WORKLOAD)

    assert view.synced is True
    assert [(b.name, b.policy_sets) for b in view.bindings] == [
        ("b-afternoon", ["set-echo"]),
        ("b-workload", ["set-reads"]),
    ]
    assert view.bindings[0].expires_at == NOW + timedelta(hours=1)
    assert [(p.binding, p.policy_set, p.index, p.policy.type) for p in view.auto_approve_if] == [
        ("b-afternoon", "set-echo", 0, PolicyKind.ARGUMENT_SCHEMA),
        ("b-workload", "set-reads", 0, PolicyKind.EXACT_ACTIONS),
    ]
    echo, reads = (p.policy for p in view.auto_approve_if)
    assert isinstance(echo, ArgumentSchemaView)
    assert (echo.actions, echo.argument_schema) == ({"everything": ["echo"]}, SCHEMA_POLICY["schema"])
    assert isinstance(reads, ExactActionsView)
    assert reads.actions == {"everything": ["add", "echo"]}
    assert "set-broken" not in view.model_dump_json()
    assert "set-missing" not in view.model_dump_json()

    other = caller_view(mixed_index, ServiceAccountRef(namespace=NAMESPACE, name="someone-else"))
    assert [b.name for b in other.bindings] == ["b-other-account"]
    assert [b.name for b in caller_view(mixed_index, CALLER).bindings] == ["b-granted"]


def test_the_operator_reads_the_same_resolution_with_each_named_set_standing(mixed_index: PolicyIndex) -> None:
    """The subject view keeps the caller's lists and adds what the caller is not shown: labels, the
    Ready verdicts, the refused set's report, and the names nothing answers to."""
    view = subject_view(mixed_index, WORKLOAD)

    assert view.auto_approve_if == caller_view(mixed_index, WORKLOAD).auto_approve_if
    afternoon, workload = view.bindings
    assert (workload.name, workload.labels, workload.ready) == (
        "b-workload",
        {"test.example/managed-by": "test-writer"},
        None,
    )
    reads, broken = workload.policy_sets
    assert (reads.name, reads.generation, reads.refused) == ("set-reads", 1, None)
    assert broken.name == "set-broken"
    assert broken.refused is not None
    assert "nope" in broken.refused
    assert workload.missing_policy_sets == ["set-missing"]
    assert (afternoon.name, afternoon.labels, afternoon.missing_policy_sets) == ("b-afternoon", {}, [])
    assert [(s.name, s.generation) for s in afternoon.policy_sets] == [("set-echo", 3)]

    account = subject_view(mixed_index, GRANTED)
    assert [b.name for b in account.bindings] == ["b-granted"]
    assert account.auto_approve_if == caller_view(mixed_index, CALLER).auto_approve_if


def test_both_views_say_nothing_auto_decides_before_sync(mixed_index: PolicyIndex) -> None:
    mixed_index.listed = False
    for view in (caller_view(mixed_index, WORKLOAD), subject_view(mixed_index, WORKLOAD)):
        assert view.synced is False
        assert (view.bindings, view.auto_approve_if) == ([], [])


async def test_an_entry_is_named_as_the_decision_names_the_policy_that_matched(
    mixed_index: PolicyIndex, github_visibility: Callable[..., RepositoryVisibilityService]
) -> None:
    """A caller reads a Decision's evidence and its own policy in one vocabulary: the entry the
    provider records in `MatchedPolicy` is the first `auto_approve_if` entry that matches."""
    view = caller_view(mixed_index, WORKLOAD)
    outcome = await PolicySetDecisionProvider(visibility=github_visibility()).decide(
        DecisionContext(
            request_id=uuid4(),
            action=ActionIdentity(group="everything", name="add"),
            arguments={},
            caller=WORKLOAD,
            bindings=resolve_bindings(mixed_index, WORKLOAD),
        )
    )

    assert outcome.evidence is not None
    matched = outcome.evidence.matched
    # `add` is not in set-echo's schema policy, so the second entry is the one that decided.
    entry = view.auto_approve_if[1]
    assert (matched.policy_set, matched.index, matched.type) == (entry.policy_set, entry.index, entry.policy.type)
    assert entry.binding in {b.name for b in outcome.evidence.bindings}


KUBERNETES_RULE: dict[str, Any] = {"type": "kubernetes_cluster_role_binding", "clusterRole": "test-reader"}
ECHO_RULE: dict[str, Any] = {
    "type": "action_auto_approve",
    "policy": {"type": "exact_actions", "actions": {"everything": ["echo"]}},
}


async def test_a_policy_binding_auto_approves_by_its_sets_action_rules_alone(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    index = index_of()
    add_rule_set(index, "ps-mixed", [KUBERNETES_RULE, ECHO_RULE])
    add_rule_set(index, "ps-kubernetes", [KUBERNETES_RULE])
    add_rule_set(index, "ps-refused", [{"type": "action_auto_approve", "policy": {"type": "nope", "actions": {}}}])
    add_policy_binding(index, "pb-workload", WORKLOAD, ["ps-mixed", "ps-kubernetes", "ps-refused", "ps-missing"])
    outcome = await PolicySetDecisionProvider(visibility=github_visibility()).decide(
        DecisionContext(
            request_id=uuid4(),
            action=ActionIdentity(group="everything", name="echo"),
            arguments={},
            caller=WORKLOAD,
            bindings=resolve_bindings(index, WORKLOAD),
        )
    )
    assert outcome.verdict is ProviderVerdict.ALLOW
    assert outcome.evidence is not None
    # The index counts every rule of the set, so it points at the rule an operator reads in it.
    assert outcome.evidence.matched == MatchedPolicy(
        namespace=NAMESPACE, policy_set="ps-mixed", source=PolicySource.RULES, index=1, type=PolicyKind.EXACT_ACTIONS
    )

    operator = subject_view(index, WORKLOAD)
    assert [(b.kind, b.name) for b in operator.bindings] == [(BindingKind.POLICY_BINDING, "pb-workload")]
    standing = {view.name: view.refused for view in operator.bindings[0].policy_sets}
    assert standing.keys() == {"ps-mixed", "ps-kubernetes", "ps-refused"}
    assert standing["ps-mixed"] is None
    assert standing["ps-refused"] is not None
    assert "nope" in standing["ps-refused"]
    assert operator.bindings[0].missing_policy_sets == ["ps-missing"]
    assert [b.policy_sets for b in caller_view(index, WORKLOAD).bindings] == [["ps-mixed", "ps-kubernetes"]]


GITHUB_READ_ACTIONS = {"github": ["get_file_contents", "search_pull_requests", "search_code"]}
GET_FILE = ActionIdentity(group="github", name="get_file_contents")
REPOSITORY_SET: SetSpec = (
    "set-repository",
    [{"type": "github_repository", "actions": GITHUB_READ_ACTIONS, "owner": "test-owner", "repository": "test-repo"}],
)
PUBLIC_SET: SetSpec = ("set-public", [{"type": "github_public_repository", "actions": GITHUB_READ_ACTIONS}])


def _context(action: ActionIdentity, arguments: dict[str, JsonValue], *sets: SetSpec) -> DecisionContext:
    index = index_of()
    for name, policies in sets:
        add_policy_set(index, name, policies)
    add_policy_binding(index, "b-github", WORKLOAD, [name for name, _ in sets])
    return DecisionContext(
        request_id=uuid4(),
        action=action,
        arguments=arguments,
        caller=WORKLOAD,
        bindings=resolve_bindings(index, WORKLOAD),
    )


async def test_github_repository_set_auto_approves_its_repository_with_the_repository_in_evidence(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    provider = PolicySetDecisionProvider(visibility=github_visibility())
    outcome = await provider.decide(
        _context(GET_FILE, {"owner": "Test-Owner", "repo": "test-repo", "path": "README.md"}, REPOSITORY_SET)
    )
    assert outcome.verdict is ProviderVerdict.ALLOW
    assert outcome.reason_code == AUTO_APPROVE_REASON
    assert outcome.evidence is not None
    assert outcome.evidence.matched == MatchedPolicy(
        namespace=NAMESPACE,
        policy_set="set-repository",
        source=PolicySource.RULES,
        index=0,
        type=PolicyKind.GITHUB_REPOSITORY,
        repository=MatchedRepository(owner="Test-Owner", repository="test-repo", confirmed_public=False),
    )


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        pytest.param(GET_FILE, {"owner": "test-owner", "repo": "other", "path": "x"}, id="other-repository"),
        pytest.param(GET_FILE, {"owner": "test-owner", "path": "x"}, id="no-repository"),
        pytest.param(
            ActionIdentity(group="github", name="search_pull_requests"),
            {"owner": "test-owner", "repo": "test-repo", "query": "repo:someone/else is:open"},
            id="smuggled-pull-request-qualifier",
        ),
        pytest.param(
            ActionIdentity(group="github", name="search_code"),
            {"query": "repo:test-owner/test-repo repo:someone/else x"},
            id="two-code-search-qualifiers",
        ),
        pytest.param(
            ActionIdentity(group="github", name="issue_read"),
            {"owner": "test-owner", "repo": "test-repo", "issue_number": 1},
            id="unlisted-action",
        ),
    ],
)
async def test_github_repository_set_misses_take_the_human_path(
    action: ActionIdentity,
    arguments: dict[str, JsonValue],
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    provider = PolicySetDecisionProvider(visibility=github_visibility())
    outcome = await provider.decide(_context(action, arguments, REPOSITORY_SET))
    assert outcome.verdict is ProviderVerdict.NO_OPINION
    assert outcome.evidence is None


async def test_github_public_repository_set_auto_approves_a_confirmed_public_repository(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    provider = PolicySetDecisionProvider(visibility=github_visibility(("someone", "public-thing")))
    outcome = await provider.decide(
        _context(GET_FILE, {"owner": "someone", "repo": "public-thing", "path": "README.md"}, PUBLIC_SET)
    )
    assert outcome.verdict is ProviderVerdict.ALLOW
    assert outcome.evidence is not None
    assert outcome.evidence.matched.type is PolicyKind.GITHUB_PUBLIC_REPOSITORY
    assert outcome.evidence.matched.repository == MatchedRepository(
        owner="someone", repository="public-thing", confirmed_public=True
    )
    assert outcome.reason_description is not None
    assert "confirmed-public repository someone/public-thing" in outcome.reason_description


@pytest.mark.parametrize(
    ("public", "unavailable"),
    [
        pytest.param((), False, id="not-public"),
        pytest.param((("someone", "public-thing"),), True, id="lookup-unavailable"),
    ],
)
async def test_github_public_repository_set_never_approves_without_a_confirmed_lookup(
    public: tuple[tuple[str, str], ...],
    unavailable: bool,
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    provider = PolicySetDecisionProvider(visibility=github_visibility(*public, unavailable=unavailable))
    outcome = await provider.decide(
        _context(GET_FILE, {"owner": "someone", "repo": "public-thing", "path": "README.md"}, PUBLIC_SET)
    )
    assert outcome.verdict is ProviderVerdict.NO_OPINION
    assert outcome.evidence is None


CALL_SERVICE = ActionIdentity(group="ha-test", name="ha_call_service")
HOME_ASSISTANT_SET: SetSpec = (
    "set-home-assistant",
    [
        {
            "type": "home_assistant_entity_control",
            "actions": {"ha-test": ["ha_call_service"]},
            "entities": {"light.test_lamp": ["turn_on"]},
        }
    ],
)
LAMP_ON: dict[str, JsonValue] = {"domain": "light", "service": "turn_on", "entity_id": "light.test_lamp"}


async def test_home_assistant_set_auto_approves_a_configured_service_call(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    provider = PolicySetDecisionProvider(visibility=github_visibility())
    outcome = await provider.decide(_context(CALL_SERVICE, LAMP_ON, HOME_ASSISTANT_SET))
    assert outcome.verdict is ProviderVerdict.ALLOW
    assert outcome.evidence is not None
    assert outcome.evidence.matched == MatchedPolicy(
        namespace=NAMESPACE,
        policy_set="set-home-assistant",
        source=PolicySource.RULES,
        index=0,
        type=PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL,
    )


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        pytest.param(CALL_SERVICE, {**LAMP_ON, "ws_command": {"type": "config/anything"}}, id="websocket-escape-hatch"),
        pytest.param(CALL_SERVICE, {**LAMP_ON, "entity_id": "light.test_other"}, id="another-entity"),
        pytest.param(ActionIdentity(group="ha-test", name="ha_bulk_control"), LAMP_ON, id="unlisted-action"),
    ],
)
async def test_home_assistant_set_misses_take_the_human_path(
    action: ActionIdentity,
    arguments: dict[str, JsonValue],
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    provider = PolicySetDecisionProvider(visibility=github_visibility())
    outcome = await provider.decide(_context(action, arguments, HOME_ASSISTANT_SET))
    assert outcome.verdict is ProviderVerdict.NO_OPINION
    assert outcome.evidence is None


def test_a_watch_that_stopped_refreshing_stops_auto_deciding() -> None:
    """The bug this guards: listing every kind once is a latch, so a replica whose watches wedged
    went on auto-approving from the snapshot it took before they did -- against bindings an operator
    may since have revoked, with nothing about the answers looking wrong."""
    index = index_of()
    add_policy_set(index, "set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    add_policy_binding(index, "b-workload", WORKLOAD, ["set-reads"])
    assert len(resolve_bindings(index, WORKLOAD)) == 1

    wedged = NOW + timedelta(seconds=index.freshness.stale_after_seconds + 1)
    index.clock = lambda: wedged
    assert index.listed is True, "the latch is still set: that is the point"
    assert index.synced is False
    assert resolve_bindings(index, WORKLOAD) == ()
    assert caller_view(index, WORKLOAD).synced is False


def test_an_account_the_label_admitted_is_refused_once_the_copy_stops_moving() -> None:
    """Admission reads the same ServiceAccounts the watch keeps; a frozen copy cannot say whether
    the label is still there, so it says no rather than vouching for what it last saw."""
    index = index_of()
    index.service_accounts[service_account_key(WORKLOAD)] = WORKLOAD
    assert index.admits(WORKLOAD) is True

    index.clock = lambda: NOW + timedelta(seconds=index.freshness.stale_after_seconds + 1)
    assert index.admits(WORKLOAD) is False


if __name__ == "__main__":
    pytest_bazel.main()
