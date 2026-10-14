"""Which PolicyBindings and egress policies the app reads for a sandbox."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel

from agentplane.egress.resources import TargetMethod
from agentplane.sandboxes.egress_views import CredentialView, EgressReader
from agentplane.sandboxes.testing.fake_inventory import (
    NAMESPACE,
    FakeCustomObjectsApi,
    egress_credential,
    egress_policy_set,
    policy_binding,
)
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf

LIVE = ServiceAccountRef(namespace=NAMESPACE, name="live")
OTHER = ServiceAccountRef(namespace=NAMESPACE, name="other")
GITHUB_RULE = {
    "hosts": ["api.github.com", "*.githubusercontent.com"],
    "methods": ["GET", "POST"],
    "credentialRef": {"name": "test-github-pat"},
}
CREDENTIAL_DESCRIPTION = "a token for the test bot account"


@pytest.fixture
def seeded(custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[("egresscredentials", "test-github-pat")] = egress_credential(
        "test-github-pat",
        secret="test-github-pat-secret",
        key="token",
        description=CREDENTIAL_DESCRIPTION,
        targets=[{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
    )
    custom_objects.objects[("policysets", "github")] = egress_policy_set("github", [GITHUB_RULE])
    custom_objects.objects[("policysets", "pypi")] = egress_policy_set("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[("policybindings", "live-seeded")] = policy_binding(
        "live-seeded", subject=LIVE.model_dump(), policy_sets=["github"]
    )
    custom_objects.objects[("policybindings", "live-expiring")] = policy_binding(
        "live-expiring",
        subject=LIVE.model_dump(),
        policy_sets=["pypi", "vanished"],
        from_git=False,
        expires_at="2026-12-01T00:00:00Z",
    )
    custom_objects.objects[("policybindings", "live-granted")] = policy_binding(
        "live-granted", subject=LIVE.model_dump(), policy_sets=["pypi"], from_git=False
    )
    custom_objects.objects[("policybindings", "other-only")] = policy_binding(
        "other-only", subject=OTHER.model_dump(), policy_sets=["pypi"]
    )


@pytest.mark.usefixtures("seeded")
async def test_bindings_for_lists_the_bindings_naming_the_subject(egress: EgressReader) -> None:
    assert [view.name for view in await egress.bindings_for(LIVE)] == ["live-expiring", "live-granted", "live-seeded"]
    assert [view.name for view in await egress.bindings_for(OTHER)] == ["other-only"]


@pytest.mark.usefixtures("seeded")
async def test_a_like_named_account_in_another_namespace_is_a_different_subject(
    egress: EgressReader, custom_objects: FakeCustomObjectsApi
) -> None:
    """A subject is a namespace and a name together: an account called "live" somewhere else is not
    this sandbox."""
    custom_objects.objects[("policybindings", "elsewhere-only")] = policy_binding(
        "elsewhere-only", subject={"namespace": "agentplane-elsewhere", "name": "live"}, policy_sets=["pypi"]
    )

    assert "elsewhere-only" not in [view.name for view in await egress.bindings_for(LIVE)]


@pytest.mark.usefixtures("seeded")
async def test_a_binding_view_carries_provenance_expiry_and_policies(egress: EgressReader) -> None:
    by_name = {view.name: view for view in await egress.bindings_for(LIVE)}

    seed = by_name["live-seeded"]
    assert seed.from_git
    assert seed.subjects == [LIVE]
    (policy,) = seed.policies
    (rule,) = policy.rules
    assert (policy.name, rule.hosts, rule.methods, rule.paths) == (
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
    assert ([policy.name for policy in expiring.policies], expiring.missing_policies) == (["pypi"], ["vanished"])

    granted = by_name["live-granted"]
    assert granted.subjects == [LIVE]


@pytest.mark.usefixtures("seeded")
async def test_only_egress_rules_make_a_policy_and_a_refused_set_reads_as_missing(
    egress: EgressReader, custom_objects: FakeCustomObjectsApi
) -> None:
    """A set another reader's rules fill is not an egress policy, and not a dangling name either; one
    whose egress rule does not parse grants nothing, which the binding reports rather than hides."""
    others = egress_policy_set("actions-only", [])
    others["spec"]["rules"] = [{"type": "action_auto_approve"}]
    custom_objects.objects[("policysets", "actions-only")] = others
    custom_objects.objects[("policysets", "broken")] = egress_policy_set("broken", [{"hosts": []}])
    custom_objects.objects[("policybindings", "live-mixed")] = policy_binding(
        "live-mixed", subject=LIVE.model_dump(), policy_sets=["pypi", "actions-only", "broken"], from_git=False
    )

    mixed = next(view for view in await egress.bindings_for(LIVE) if view.name == "live-mixed")

    assert ([policy.name for policy in mixed.policies], mixed.missing_policies) == (["pypi"], ["broken"])
    assert {policy.name for policy in await egress.list_policies()} == {"github", "pypi"}


@pytest.mark.usefixtures("seeded")
async def test_list_policies_summarises_every_rule(egress: EgressReader) -> None:
    policies = {policy.name: policy for policy in await egress.list_policies()}

    assert set(policies) == {"github", "pypi"}
    (open_rule,) = policies["pypi"].rules
    assert (open_rule.hosts, open_rule.methods, open_rule.credential) == (["pypi.org"], None, None)


async def test_authenticated_workload_source_projects_no_token_or_secret_reference(
    egress: EgressReader, custom_objects: FakeCustomObjectsApi
) -> None:
    custom_objects.objects[("egresscredentials", "workload")] = {
        "apiVersion": "agentplane.allegedly.works/v1alpha1",
        "kind": "EgressCredential",
        "metadata": {"name": "workload"},
        "spec": {
            "source": {"authenticatedWorkloadToken": {}},
            "description": "the authenticated caller",
            "targets": [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
        },
    }
    custom_objects.objects[("policysets", "workload")] = egress_policy_set(
        "workload", [{"hosts": ["agentplane.internal"], "credentialRef": {"name": "workload"}}]
    )

    credential = (await egress.list_policies())[0].rules[0].credential

    assert credential is not None
    assert (credential.name, credential.secret, credential.key) == ("workload", None, None)


async def test_a_rule_naming_a_credential_the_namespace_does_not_hold_says_which_name_dangles(
    egress: EgressReader, custom_objects: FakeCustomObjectsApi
) -> None:
    """The proxy substitutes nothing for it, so the page must not read as a rule that wanted none."""
    custom_objects.objects[("policysets", "orphan")] = egress_policy_set(
        "orphan", [{"hosts": ["api.github.com"], "credentialRef": {"name": "gone"}}]
    )

    (rule,) = {policy.name: policy for policy in await egress.list_policies()}["orphan"].rules

    assert rule.credential is None
    assert rule.missing_credential == "gone"


@pytest.mark.usefixtures("seeded")
async def test_operator_json_target_has_no_nullable_header_or_scheme(
    egress: EgressReader, custom_objects: FakeCustomObjectsApi
) -> None:
    target = {"method": "jsonField", "field": "password"}
    custom_objects.objects[("egresscredentials", "test-github-pat")] = egress_credential(
        "test-github-pat",
        secret="test-github-pat-secret",
        key="token",
        description=CREDENTIAL_DESCRIPTION,
        targets=[target],
    )
    views = await egress.bindings_for(LIVE)
    seeded = next(view for view in views if view.name == "live-seeded")
    credential = seeded.policies[0].rules[0].credential
    assert credential is not None
    assert credential.model_dump(mode="json")["targets"] == [target]


def test_operator_schema_preserves_the_target_discriminator() -> None:
    items = CredentialView.model_json_schema()["properties"]["targets"]["items"]
    assert items["discriminator"]["propertyName"] == "method"
    assert set(items["discriminator"]["mapping"]) == {method.value for method in TargetMethod}
    assert len(items["oneOf"]) == len(TargetMethod)


if __name__ == "__main__":
    pytest_bazel.main()
