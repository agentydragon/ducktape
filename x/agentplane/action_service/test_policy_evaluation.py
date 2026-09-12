"""Each policy kind's own test, how a caller's bindings resolve from the index at one instant, and
what the caller and the operator read of that resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import JsonValue

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import PolicyKind, SandboxCaller, ServiceAccountCaller, ServiceAccountRef
from x.agentplane.action_service.policy_evaluation import (
    Matched,
    NotMatched,
    PolicySetDecisionProvider,
    evaluate,
    resolve_bindings,
)
from x.agentplane.action_service.policy_informer import PolicyIndex, namespaced_key
from x.agentplane.action_service.policy_resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
    ObjectMeta,
    Status,
    parse_binding,
    parse_policy_set,
)
from x.agentplane.action_service.policy_view import ArgumentSchemaView, ExactActionsView, caller_view, subject_view
from x.agentplane.action_service.providers import DecisionContext

NAMESPACE = "agentplane-test"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SANDBOX = SandboxCaller(namespace=NAMESPACE, sandbox_uid="sandbox-uid-1")
ACCOUNT = ServiceAccountRef(namespace=NAMESPACE, name="test-caller")
CALLER = ServiceAccountCaller(service_account=ACCOUNT, grant_revision=1)
ECHO = ActionIdentity(group="everything", name="echo")

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


def _meta(
    name: str, *, generation: int = 1, version: str = "1", labels: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": NAMESPACE,
        "uid": f"uid-{name}",
        "generation": generation,
        "resourceVersion": version,
        **({"labels": labels} if labels is not None else {}),
    }


def policy_set(name: str, policies: list[dict[str, Any]], **meta: Any) -> ActionPolicySet | InvalidResource:
    return parse_policy_set({"metadata": _meta(name, **meta), "spec": {"autoApproveIf": policies}})


def binding(
    name: str, subject: dict[str, Any], sets: list[str], *, expires_at: str | None = None, **meta: Any
) -> ActionPolicyBinding | InvalidResource:
    spec: dict[str, Any] = {"subject": subject, "policySets": sets}
    if expires_at is not None:
        spec["expiresAt"] = expires_at
    return parse_binding({"metadata": _meta(name, **meta), "spec": spec})


def index_of(*objects: ActionPolicySet | ActionPolicyBinding | InvalidResource, synced: bool = True) -> PolicyIndex:
    index = PolicyIndex(synced=synced)
    for obj in objects:
        key = namespaced_key(obj.metadata.namespace, obj.metadata.name)
        if isinstance(obj, ActionPolicySet) or (
            isinstance(obj, InvalidResource) and obj.metadata.name.startswith("set")
        ):
            index.policy_sets[key] = obj
        else:
            index.bindings[key] = obj
    return index


@pytest.mark.parametrize(
    ("arguments", "matched"),
    [
        ({"message": "hi"}, True),
        ({"message": "hi", "count": 2}, True),
        ({}, False),  # `required` decides presence; `properties` alone never does.
        ({"message": "too long"}, False),
        ({"message": "hi", "count": "2"}, False),
        ({"message": "hi", "extra": True}, False),
    ],
)
def test_argument_schema_has_plain_json_schema_semantics(arguments: dict[str, JsonValue], matched: bool) -> None:
    parsed = policy_set("set-a", [SCHEMA_POLICY])
    assert isinstance(parsed, ActionPolicySet)
    (policy,) = parsed.spec.auto_approve_if
    assert isinstance(evaluate(policy, ECHO, arguments), Matched if matched else NotMatched)


@pytest.mark.parametrize(
    ("action", "matched"),
    [
        (ECHO, True),
        (ActionIdentity(group="everything", name="add"), False),
        (ActionIdentity(group="other", name="echo"), False),
    ],
)
def test_exact_actions_matches_by_name_alone(action: ActionIdentity, matched: bool) -> None:
    parsed = policy_set("set-a", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    assert isinstance(parsed, ActionPolicySet)
    (policy,) = parsed.spec.auto_approve_if
    assert isinstance(evaluate(policy, action, {"anything": [1, 2]}), Matched if matched else NotMatched)


def test_resolution_takes_unexpired_valid_bindings_naming_the_caller_with_their_existing_sets() -> None:
    reads = policy_set("set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    broken = policy_set("set-broken", [{"type": "nope", "actions": {"everything": ["echo"]}}])
    index = index_of(
        reads,
        broken,
        binding(
            "b-sandbox",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads", "set-broken", "set-missing"],
        ),
        binding("b-account", {"serviceAccount": {"namespace": NAMESPACE, "name": ACCOUNT.name}}, ["set-reads"]),
        binding(
            "b-expired",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads"],
            expires_at=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        binding(
            "b-later",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads"],
            expires_at=(NOW + timedelta(hours=1)).isoformat(),
        ),
        binding("b-other-uid", {"sandbox": {"name": "coder", "uid": "someone-else"}}, ["set-reads"]),
        binding("b-invalid", {"sandbox": {"name": "coder"}}, ["set-reads"]),
    )
    for_sandbox = resolve_bindings(index, SANDBOX, NOW)
    assert [resolved.binding.metadata.name for resolved in for_sandbox] == ["b-later", "b-sandbox"]
    assert [[s.metadata.name for s in resolved.policy_sets] for resolved in for_sandbox] == [
        ["set-reads"],
        ["set-reads"],
    ]
    for_account = resolve_bindings(index, CALLER, NOW)
    assert [resolved.binding.metadata.name for resolved in for_account] == ["b-account"]
    assert (
        resolve_bindings(index, SandboxCaller(namespace="agentplane-other", sandbox_uid=SANDBOX.sandbox_uid), NOW) == ()
    )
    assert (
        resolve_bindings(
            index,
            ServiceAccountCaller(service_account=ServiceAccountRef(namespace=NAMESPACE, name="x"), grant_revision=3),
            NOW,
        )
        == ()
    )


def test_nothing_resolves_before_the_informer_has_synced() -> None:
    index = index_of(
        policy_set("set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}]),
        binding("b-sandbox", {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}}, ["set-reads"]),
        synced=False,
    )
    assert resolve_bindings(index, SANDBOX, NOW) == ()
    index.synced = True
    assert len(resolve_bindings(index, SANDBOX, NOW)) == 1


@pytest.fixture
def mixed_index() -> PolicyIndex:
    """One Sandbox with a valid, a refused and a missing set on one binding, a second binding of
    its own, and the bindings the resolution skips: expired, another Sandbox's, a ServiceAccount's."""
    return index_of(
        policy_set("set-reads", [{"type": "exact_actions", "actions": {"everything": ["echo", "add"]}}]),
        policy_set("set-echo", [SCHEMA_POLICY], generation=3),
        policy_set("set-broken", [{"type": "nope", "actions": {"everything": ["echo"]}}]),
        binding(
            "b-sandbox",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads", "set-broken", "set-missing"],
            labels={"test.example/managed-by": "test-writer"},
        ),
        binding(
            "b-afternoon",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-echo"],
            expires_at=(NOW + timedelta(hours=1)).isoformat(),
        ),
        binding(
            "b-expired",
            {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}},
            ["set-reads"],
            expires_at=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        binding("b-other-uid", {"sandbox": {"name": "coder", "uid": "someone-else"}}, ["set-reads"]),
        binding("b-account", {"serviceAccount": {"namespace": NAMESPACE, "name": ACCOUNT.name}}, ["set-echo"]),
    )


def test_a_caller_reads_its_own_resolution_and_nothing_of_the_sets_it_cannot_use(mixed_index: PolicyIndex) -> None:
    """The caller view names the bindings admission resolves and, per binding, only the sets that
    contribute: a refused or missing set is simply not there, and another subject's bindings never are."""
    view = caller_view(mixed_index, SANDBOX, NOW)

    assert view.synced is True
    assert [(b.name, b.policy_sets) for b in view.bindings] == [
        ("b-afternoon", ["set-echo"]),
        ("b-sandbox", ["set-reads"]),
    ]
    assert view.bindings[0].expires_at == NOW + timedelta(hours=1)
    assert [(p.binding, p.policy_set, p.index, p.policy.type) for p in view.auto_approve_if] == [
        ("b-afternoon", "set-echo", 0, PolicyKind.ARGUMENT_SCHEMA),
        ("b-sandbox", "set-reads", 0, PolicyKind.EXACT_ACTIONS),
    ]
    echo, reads = (p.policy for p in view.auto_approve_if)
    assert isinstance(echo, ArgumentSchemaView)
    assert (echo.actions, echo.argument_schema) == ({"everything": ["echo"]}, SCHEMA_POLICY["schema"])
    assert isinstance(reads, ExactActionsView)
    assert reads.actions == {"everything": ["add", "echo"]}
    assert (view.auto_deny_if, view.auto_deny_unless) == ([], [])
    assert "set-broken" not in view.model_dump_json()
    assert "set-missing" not in view.model_dump_json()

    other = caller_view(mixed_index, SandboxCaller(namespace=NAMESPACE, sandbox_uid="someone-else"), NOW)
    assert [b.name for b in other.bindings] == ["b-other-uid"]
    assert [b.name for b in caller_view(mixed_index, CALLER, NOW).bindings] == ["b-account"]


def test_the_operator_reads_the_same_resolution_with_each_named_set_standing(mixed_index: PolicyIndex) -> None:
    """The subject view keeps the caller's lists and adds what the caller is not shown: labels, the
    Ready verdicts, the refused set's report, and the names nothing answers to."""
    view = subject_view(mixed_index, SANDBOX, NOW)

    assert view.auto_approve_if == caller_view(mixed_index, SANDBOX, NOW).auto_approve_if
    afternoon, sandbox = view.bindings
    assert (sandbox.name, sandbox.labels, sandbox.ready) == (
        "b-sandbox",
        {"test.example/managed-by": "test-writer"},
        None,
    )
    reads, broken = sandbox.policy_sets
    assert (reads.name, reads.generation, reads.refused) == ("set-reads", 1, None)
    assert broken.name == "set-broken"
    assert broken.refused is not None
    assert "nope" in broken.refused
    assert sandbox.missing_policy_sets == ["set-missing"]
    assert (afternoon.name, afternoon.labels, afternoon.missing_policy_sets) == ("b-afternoon", {}, [])
    assert [(s.name, s.generation) for s in afternoon.policy_sets] == [("set-echo", 3)]

    account = subject_view(mixed_index, ACCOUNT, NOW)
    assert [b.name for b in account.bindings] == ["b-account"]
    assert account.auto_approve_if == caller_view(mixed_index, CALLER, NOW).auto_approve_if


def test_both_views_say_nothing_auto_decides_before_sync(mixed_index: PolicyIndex) -> None:
    mixed_index.synced = False
    for view in (caller_view(mixed_index, SANDBOX, NOW), subject_view(mixed_index, SANDBOX, NOW)):
        assert view.synced is False
        assert (view.bindings, view.auto_approve_if) == ([], [])


async def test_an_entry_is_named_as_the_decision_names_the_policy_that_matched(mixed_index: PolicyIndex) -> None:
    """A caller reads a Decision's evidence and its own policy in one vocabulary: the entry the
    provider records in `MatchedPolicy` is the first `auto_approve_if` entry that matches."""
    view = caller_view(mixed_index, SANDBOX, NOW)
    outcome = await PolicySetDecisionProvider().decide(
        DecisionContext(
            request_id=uuid4(),
            action=ActionIdentity(group="everything", name="add"),
            arguments={},
            caller=SANDBOX,
            bindings=resolve_bindings(mixed_index, SANDBOX, NOW),
        )
    )

    assert outcome.evidence is not None
    matched = outcome.evidence.matched
    # `add` is not in set-echo's schema policy, so the second entry is the one that decided.
    entry = view.auto_approve_if[1]
    assert (matched.policy_set, matched.index, matched.type) == (entry.policy_set, entry.index, entry.policy.type)
    assert entry.binding in {b.name for b in outcome.evidence.bindings}


def test_invalid_resource_keeps_metadata_for_status_reporting() -> None:
    broken = binding("b-invalid", {"sandbox": {"name": "coder"}}, ["set-reads"])
    assert isinstance(broken, InvalidResource)
    assert broken.metadata == ObjectMeta(
        name="b-invalid", namespace=NAMESPACE, uid="uid-b-invalid", generation=1, resource_version="1"
    )
    assert broken.status == Status()


if __name__ == "__main__":
    pytest_bazel.main()
