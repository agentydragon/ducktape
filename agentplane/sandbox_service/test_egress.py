"""Which PolicyBindings the app shows a sandbox, what it reads off them, and what it writes back."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel

from agentplane.egress.resources import TargetMethod
from agentplane.proto.sandbox_service_pb2 import Sandbox, ServiceAccount
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.egress_views import (
    BindingNotFoundError,
    CredentialView,
    FluxOwnedBindingError,
    SandboxOwnedBindingError,
    UnknownPolicyError,
)
from agentplane.sandbox_service.testing.fake_inventory import (
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
LIVE_SANDBOX = Sandbox(
    name="live", uid="test-live-uid", service_account=ServiceAccount(namespace=NAMESPACE, name="live")
)

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
async def test_bindings_for_lists_the_bindings_naming_the_subject(egress: EgressInventory) -> None:
    assert [view.name for view in await egress.bindings_for(LIVE)] == ["live-expiring", "live-granted", "live-seeded"]
    assert [view.name for view in await egress.bindings_for(OTHER)] == ["other-only"]


@pytest.mark.usefixtures("seeded")
async def test_a_like_named_account_in_another_namespace_is_a_different_subject(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """A subject is a namespace and a name together: an account called "live" somewhere else is not
    this sandbox."""
    custom_objects.objects[("policybindings", "elsewhere-only")] = policy_binding(
        "elsewhere-only", subject={"namespace": "agentplane-elsewhere", "name": "live"}, policy_sets=["pypi"]
    )

    assert "elsewhere-only" not in [view.name for view in await egress.bindings_for(LIVE)]


@pytest.mark.usefixtures("seeded")
async def test_a_binding_view_carries_provenance_expiry_and_policies(egress: EgressInventory) -> None:
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
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
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
async def test_revoke_deletes_a_runtime_binding_and_refuses_one_from_git(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Deleting the binding is the whole revocation; a Flux-applied one would come straight back, so
    the app refuses it instead of deleting an object the next reconcile re-creates."""
    await egress.revoke("live-granted")

    assert ("policybindings", "live-granted") not in custom_objects.objects
    with pytest.raises(FluxOwnedBindingError):
        await egress.revoke("live-seeded")
    assert ("policybindings", "live-seeded") in custom_objects.objects
    with pytest.raises(BindingNotFoundError):
        await egress.revoke("live-granted")


@pytest.mark.usefixtures("seeded")
async def test_the_sandbox_own_binding_is_not_listed_and_revoke_refuses_it(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """The Sandbox controller keeps that binding equal to the Sandbox's spec, so deleting it would
    be undone at once and look like a revocation that did not hold."""
    custom_objects.objects[("policybindings", "live")] = policy_binding(
        "live", subject=LIVE.model_dump(), policy_sets=["pypi"], from_git=False, sandbox_controlled="live"
    )

    assert "live" not in [view.name for view in await egress.bindings_for(LIVE)]
    with pytest.raises(SandboxOwnedBindingError):
        await egress.revoke("live")
    assert ("policybindings", "live") in custom_objects.objects


@pytest.mark.usefixtures("seeded")
async def test_grant_creates_a_binding_the_sandbox_owns(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Creating the binding is the grant: nothing has to answer it afterwards. The API server names
    it, so the app learns the name from what it gets back."""
    granted = await egress.grant(LIVE_SANDBOX, ["pypi", "github"])

    assert granted.name.startswith("live-")
    created = custom_objects.objects[("policybindings", granted.name)]
    (owner,) = created["metadata"]["ownerReferences"]
    assert owner == {
        "apiVersion": SANDBOX_API_VERSION,
        "kind": SANDBOX_KIND,
        "name": "live",
        "uid": str(LIVE_SANDBOX.uid),
        "controller": False,
        "blockOwnerDeletion": False,
    }
    assert created["spec"] == {"subject": LIVE.model_dump(), "policySets": ["pypi", "github"]}
    # And the app reads its own grant back like any other binding, not from git.
    (read_back,) = [view for view in await egress.bindings_for(LIVE) if view.name == granted.name]
    assert (read_back.from_git, [policy.name for policy in read_back.policies]) == (False, ["pypi", "github"])


@pytest.mark.usefixtures("seeded")
async def test_a_second_grant_is_another_binding_and_not_an_edit_of_the_first(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """`expiresAt` is per binding, so granting a running sandbox cannot append to a binding that
    carries other policies: at the deadline it would take those away too."""
    first = await egress.grant(LIVE_SANDBOX, ["pypi"])
    second = await egress.grant(LIVE_SANDBOX, ["github"])

    assert first.name != second.name
    assert custom_objects.objects[("policybindings", first.name)]["spec"]["policySets"] == ["pypi"]
    assert custom_objects.objects[("policybindings", second.name)]["spec"]["policySets"] == ["github"]
    # Both name the sandbox, so the proxy's union over its bindings is what composes them.
    assert {first.name, second.name} <= {view.name for view in await egress.bindings_for(LIVE)}


@pytest.mark.usefixtures("seeded")
async def test_a_grant_naming_a_policy_the_namespace_lacks_writes_nothing(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """A binding may carry a name nothing answers to — the proxy reports that as `MissingPolicy` —
    but the app will not mint one, so a mistyped grant leaves no rule behind to puzzle over."""
    before = set(custom_objects.objects)

    with pytest.raises(UnknownPolicyError) as refused:
        await egress.grant(LIVE_SANDBOX, ["pypi", "vanished"])

    assert refused.value.names == ["vanished"]
    assert set(custom_objects.objects) == before


@pytest.mark.usefixtures("seeded")
async def test_list_policies_summarises_every_rule(egress: EgressInventory) -> None:
    policies = {policy.name: policy for policy in await egress.list_policies()}

    assert set(policies) == {"github", "pypi"}
    (open_rule,) = policies["pypi"].rules
    assert (open_rule.hosts, open_rule.methods, open_rule.credential) == (["pypi.org"], None, None)


async def test_authenticated_workload_source_projects_no_token_or_secret_reference(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
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
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
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
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
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
