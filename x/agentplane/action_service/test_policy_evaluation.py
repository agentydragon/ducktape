"""How a caller's bindings resolve from the index at one instant, and the provider deciding the
GitHub repository kinds over them: a match carries the repository in evidence, every miss is no
opinion."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import JsonValue

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import (
    MatchedPolicy,
    MatchedRepository,
    PolicyKind,
    ProviderVerdict,
    SandboxCaller,
    ServiceAccountCaller,
    ServiceAccountRef,
)
from x.agentplane.action_service.policies.resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    InvalidResource,
    ObjectMeta,
    Status,
    parse_binding,
    parse_policy_set,
)
from x.agentplane.action_service.policy_evaluation import (
    AUTO_APPROVE_REASON,
    PolicySetDecisionProvider,
    resolve_bindings,
)
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.action_service.providers import DecisionContext, ResolvedBinding

NAMESPACE = "agentplane-test"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SANDBOX = SandboxCaller(namespace=NAMESPACE, sandbox_uid="sandbox-uid-1")
ACCOUNT = ServiceAccountRef(namespace=NAMESPACE, name="test-caller")
CALLER = ServiceAccountCaller(service_account=ACCOUNT, grant_revision=1)


def _meta(name: str, *, generation: int = 1, version: str = "1") -> dict[str, Any]:
    return {
        "name": name,
        "namespace": NAMESPACE,
        "uid": f"uid-{name}",
        "generation": generation,
        "resourceVersion": version,
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
        key = obj.namespaced_name
        if isinstance(obj, ActionPolicySet) or (
            isinstance(obj, InvalidResource) and obj.metadata.name.startswith("set")
        ):
            index.policy_sets[key] = obj
        else:
            index.bindings[key] = obj
    return index


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


def test_invalid_resource_keeps_metadata_for_status_reporting() -> None:
    broken = binding("b-invalid", {"sandbox": {"name": "coder"}}, ["set-reads"])
    assert isinstance(broken, InvalidResource)
    assert broken.metadata == ObjectMeta(
        name="b-invalid", namespace=NAMESPACE, uid="uid-b-invalid", generation=1, resource_version="1"
    )
    assert broken.status == Status()


GITHUB_READ_ACTIONS = {"github": ["get_file_contents", "search_pull_requests", "search_code"]}
GET_FILE = ActionIdentity(group="github", name="get_file_contents")
REPOSITORY_SET = policy_set(
    "set-repository",
    [{"type": "github_repository", "actions": GITHUB_READ_ACTIONS, "owner": "test-owner", "repository": "test-repo"}],
)
PUBLIC_SET = policy_set("set-public", [{"type": "github_public_repository", "actions": GITHUB_READ_ACTIONS}])


def _context(action: ActionIdentity, arguments: dict[str, JsonValue], *sets: ActionPolicySet) -> DecisionContext:
    bound = binding(
        "b-github", {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}}, [s.metadata.name for s in sets]
    )
    assert isinstance(bound, ActionPolicyBinding)
    return DecisionContext(
        request_id=uuid4(),
        action=action,
        arguments=arguments,
        caller=SANDBOX,
        bindings=(ResolvedBinding(binding=bound, policy_sets=sets),),
    )


async def test_github_repository_set_auto_approves_its_repository_with_the_repository_in_evidence(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    assert isinstance(REPOSITORY_SET, ActionPolicySet)
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
        source="autoApproveIf",
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
    assert isinstance(REPOSITORY_SET, ActionPolicySet)
    provider = PolicySetDecisionProvider(visibility=github_visibility())
    outcome = await provider.decide(_context(action, arguments, REPOSITORY_SET))
    assert outcome.verdict is ProviderVerdict.NO_OPINION
    assert outcome.evidence is None


async def test_github_public_repository_set_auto_approves_a_confirmed_public_repository(
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    assert isinstance(PUBLIC_SET, ActionPolicySet)
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
    assert isinstance(PUBLIC_SET, ActionPolicySet)
    provider = PolicySetDecisionProvider(visibility=github_visibility(*public, unavailable=unavailable))
    outcome = await provider.decide(
        _context(GET_FILE, {"owner": "someone", "repo": "public-thing", "path": "README.md"}, PUBLIC_SET)
    )
    assert outcome.verdict is ProviderVerdict.NO_OPINION
    assert outcome.evidence is None


if __name__ == "__main__":
    pytest_bazel.main()
