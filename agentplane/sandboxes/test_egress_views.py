"""Which PolicyBindings the app reads for a ServiceAccount, and how it shows their sets and verdicts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel

from agentplane.egress.resources import CREDENTIALS_PLURAL, TargetMethod
from agentplane.policy_sets import ConditionStatus
from agentplane.sandboxes.egress_views import (
    BindingProvenance,
    BindingView,
    CredentialView,
    EgressReader,
    EgressRuleView,
    OtherRuleView,
    matching_bindings,
)
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf

LIVE = ServiceAccountRef(namespace="test-sandboxes", name="live")
OTHER = ServiceAccountRef(namespace="test-sandboxes", name="other")
GITHUB_RULE = {
    "hosts": ["api.github.com", "*.githubusercontent.com"],
    "methods": ["GET", "POST"],
    "credentialRef": {"name": "test-github-pat"},
}
CREDENTIAL_DESCRIPTION = "a token for the test bot account"
GITHUB_PAT = {"secretRef": {"name": "test-github-pat-secret", "key": "token"}}


def only_egress_rule(binding: BindingView) -> EgressRuleView:
    (policy_set_view,) = binding.policy_sets
    (rule,) = policy_set_view.rules
    assert isinstance(rule, EgressRuleView)
    return rule


@pytest.fixture
async def seeded(namespace: SandboxNamespace) -> None:
    await namespace.egress_credential(
        "test-github-pat",
        source=GITHUB_PAT,
        description=CREDENTIAL_DESCRIPTION,
        targets=[{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
    )
    await namespace.egress_policy_set("github", [GITHUB_RULE])
    await namespace.egress_policy_set("pypi", [{"hosts": ["pypi.org"]}])
    await namespace.policy_binding("live-seeded", subject=LIVE, policy_sets=["github"])
    await namespace.policy_binding(
        "live-expiring",
        subject=LIVE,
        policy_sets=["pypi", "vanished"],
        from_git=False,
        expires_at="2026-12-01T00:00:00Z",
    )
    await namespace.policy_binding("live-granted", subject=LIVE, policy_sets=["pypi"], from_git=False)
    await namespace.policy_binding("other-only", subject=OTHER, policy_sets=["pypi"])


@pytest.mark.usefixtures("seeded")
async def test_bindings_for_lists_the_bindings_naming_the_subject(egress: EgressReader) -> None:
    assert [view.name for view in await egress.bindings_for(LIVE)] == ["live-expiring", "live-granted", "live-seeded"]
    assert [view.name for view in await egress.bindings_for(OTHER)] == ["other-only"]


@pytest.mark.usefixtures("seeded")
async def test_a_like_named_account_in_another_namespace_is_a_different_subject(
    egress: EgressReader, namespace: SandboxNamespace
) -> None:
    """A subject is a namespace and a name together: an account called "live" somewhere else is not
    this sandbox."""
    await namespace.policy_binding(
        "elsewhere-only", subject=ServiceAccountRef(namespace="test-elsewhere", name="live"), policy_sets=["pypi"]
    )

    assert "elsewhere-only" not in [view.name for view in await egress.bindings_for(LIVE)]


@pytest.mark.usefixtures("seeded")
async def test_a_binding_view_carries_provenance_expiry_and_its_egress_rules(egress: EgressReader) -> None:
    by_name = {view.name: view for view in await egress.bindings_for(LIVE)}

    seed = by_name["live-seeded"]
    assert (seed.provenance, seed.subjects) == (BindingProvenance.GIT, [LIVE])
    rule = only_egress_rule(seed)
    assert (seed.policy_sets[0].name, rule.hosts, rule.methods, rule.paths) == (
        "github",
        ["api.github.com", "*.githubusercontent.com"],
        ["GET", "POST"],
        None,
    )
    assert rule.credential is not None
    assert (rule.credential.name, rule.credential.description) == ("test-github-pat", CREDENTIAL_DESCRIPTION)
    assert (rule.credential.secret, rule.credential.key) == ("test-github-pat-secret", "token")
    assert [target.model_dump(mode="json") for target in rule.credential.targets] == [
        {"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}
    ]
    assert rule.missing_credential is None

    expiring = by_name["live-expiring"]
    assert expiring.expires_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert ([policy_set.name for policy_set in expiring.policy_sets], expiring.missing_policy_sets) == (
        ["pypi"],
        ["vanished"],
    )
    open_rule = only_egress_rule(by_name["live-granted"])
    assert (open_rule.hosts, open_rule.methods, open_rule.credential) == (["pypi.org"], None, None)


@pytest.mark.usefixtures("seeded")
async def test_provenance_tells_git_from_the_sandboxs_own_from_an_operators_grant(
    egress: EgressReader, namespace: SandboxNamespace
) -> None:
    await namespace.policy_binding(
        "live-own", subject=LIVE, policy_sets=["pypi"], from_git=False, sandbox_controlled="live"
    )

    assert {view.name: view.provenance for view in await egress.bindings_for(LIVE)} == {
        "live-expiring": BindingProvenance.OPERATOR,
        "live-granted": BindingProvenance.OPERATOR,
        "live-own": BindingProvenance.SANDBOX,
        "live-seeded": BindingProvenance.GIT,
    }


@pytest.mark.usefixtures("seeded")
async def test_a_set_shows_every_rule_of_every_type(egress: EgressReader, namespace: SandboxNamespace) -> None:
    """Rules another reader enforces are shown as the set states them, not hidden."""
    await namespace.policy_set(
        "mixed",
        [
            {
                "type": "action_auto_approve",
                "policy": {"type": "exact_actions", "actions": {"test_review": ["record"]}},
            },
            {"type": "egress", "hosts": ["pypi.org"]},
        ],
    )
    await namespace.policy_binding("live-mixed", subject=LIVE, policy_sets=["mixed", "vanished"], from_git=False)

    mixed = next(view for view in await egress.bindings_for(LIVE) if view.name == "live-mixed")

    assert [policy_set.name for policy_set in mixed.policy_sets] == ["mixed"]
    assert mixed.missing_policy_sets == ["vanished"]
    action, egress_rule = mixed.policy_sets[0].rules
    assert isinstance(action, OtherRuleView)
    assert (action.number, action.type, action.fields) == (
        0,
        "action_auto_approve",
        {"policy": {"type": "exact_actions", "actions": {"test_review": ["record"]}}},
    )
    assert isinstance(egress_rule, EgressRuleView)
    assert (egress_rule.number, egress_rule.hosts) == (1, ["pypi.org"])


def test_a_set_whose_egress_rule_does_not_parse_is_refused_and_says_why() -> None:
    """It grants the egress proxy nothing, which the binding reports rather than hides. The CRD admits
    no such rule, so only a set stored under an older schema reaches a reader with one: the projection
    is given that object directly."""
    metadata = {"namespace": "test-sandboxes", "uid": "test-uid", "generation": 1, "resourceVersion": "1"}
    broken = {"metadata": {"name": "broken", **metadata}, "spec": {"rules": [{"type": "egress", "hosts": []}]}}
    binding = {
        "metadata": {"name": "live-broken", **metadata},
        "spec": {"subject": LIVE.model_dump(), "policySets": ["broken"]},
    }

    (view,) = matching_bindings([binding], [broken], [], subject=LIVE)

    (refused,) = view.policy_sets
    assert (refused.name, refused.rules, refused.refused is not None) == ("broken", [], True)


@pytest.mark.usefixtures("seeded")
async def test_a_binding_view_carries_each_readers_verdict_by_type(
    egress: EgressReader, namespace: SandboxNamespace
) -> None:
    def verdict(condition_type: str, *, status: str, generation: int) -> dict[str, object]:
        return {
            "type": condition_type,
            "status": status,
            "reason": "Test",
            "message": f"{condition_type} says {status}",
            "observedGeneration": generation,
            "lastTransitionTime": "2026-09-01T12:00:00Z",
        }

    await namespace.policy_binding(
        "live-judged",
        subject=LIVE,
        policy_sets=["pypi"],
        conditions=[
            verdict("EgressReady", status="False", generation=1),
            verdict("ActionsReady", status="True", generation=1),
        ],
    )

    by_name = {view.name: view for view in await egress.bindings_for(LIVE)}

    judged = by_name["live-judged"]
    assert [(c.type, c.status, c.message, c.observed_generation) for c in judged.conditions] == [
        ("ActionsReady", ConditionStatus.TRUE, "ActionsReady says True", 1),
        ("EgressReady", ConditionStatus.FALSE, "EgressReady says False", 1),
    ]
    assert judged.generation == 1
    assert by_name["live-seeded"].conditions == []


async def test_authenticated_workload_source_projects_no_token_or_secret_reference(
    egress: EgressReader, namespace: SandboxNamespace
) -> None:
    await namespace.egress_credential(
        "workload",
        source={"authenticatedWorkloadToken": {}},
        description="the authenticated caller",
        targets=[{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
    )
    await namespace.egress_policy_set(
        "workload", [{"hosts": ["agentplane.internal"], "credentialRef": {"name": "workload"}}]
    )
    await namespace.policy_binding("live-workload", subject=LIVE, policy_sets=["workload"])

    (binding,) = await egress.bindings_for(LIVE)
    credential = only_egress_rule(binding).credential

    assert credential is not None
    assert (credential.name, credential.secret, credential.key) == ("workload", None, None)


async def test_a_rule_naming_a_credential_the_namespace_does_not_hold_says_which_name_dangles(
    egress: EgressReader, namespace: SandboxNamespace
) -> None:
    """The proxy substitutes nothing for it, so the page must not read as a rule that wanted none."""
    await namespace.egress_policy_set("orphan", [{"hosts": ["api.github.com"], "credentialRef": {"name": "gone"}}])
    await namespace.policy_binding("live-orphan", subject=LIVE, policy_sets=["orphan"])

    (binding,) = await egress.bindings_for(LIVE)
    rule = only_egress_rule(binding)

    assert rule.credential is None
    assert rule.missing_credential == "gone"


@pytest.mark.usefixtures("seeded")
async def test_operator_json_target_has_no_nullable_header_or_scheme(
    egress: EgressReader, namespace: SandboxNamespace
) -> None:
    target = {"method": "jsonField", "field": "password"}
    await namespace.patch(CREDENTIALS_PLURAL, "test-github-pat", {"spec": {"targets": [target]}})
    views = await egress.bindings_for(LIVE)
    credential = only_egress_rule(next(view for view in views if view.name == "live-seeded")).credential
    assert credential is not None
    assert credential.model_dump(mode="json")["targets"] == [target]


def test_operator_schema_preserves_the_target_discriminator() -> None:
    items = CredentialView.model_json_schema()["properties"]["targets"]["items"]
    assert items["discriminator"]["propertyName"] == "method"
    assert set(items["discriminator"]["mapping"]) == {method.value for method in TargetMethod}
    assert len(items["oneOf"]) == len(TargetMethod)


if __name__ == "__main__":
    pytest_bazel.main()
