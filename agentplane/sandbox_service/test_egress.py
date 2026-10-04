"""Which bindings the app shows a sandbox, what it reads off them, and what it writes back."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.egress.resources import TargetMethod
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.egress_views import (
    BindingNotFoundError,
    CredentialView,
    FluxOwnedBindingError,
    UnknownPolicyError,
)
from agentplane.sandbox_service.protocol_pb2 import Sandbox, ServiceAccount
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    FakeCustomObjectsApi,
    egress_binding,
    egress_credential,
    egress_policy,
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


def _seed(custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[("egresscredentials", "test-github-pat")] = egress_credential(
        "test-github-pat",
        secret="test-github-pat-secret",
        key="token",
        description=CREDENTIAL_DESCRIPTION,
        targets=[{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
    )
    custom_objects.objects[("egresspolicies", "github")] = egress_policy("github", [GITHUB_RULE])
    custom_objects.objects[("egresspolicies", "pypi")] = egress_policy("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[("egressbindings", "live-seeded")] = egress_binding(
        "live-seeded", subjects=[LIVE.model_dump()], policies=["github"]
    )
    custom_objects.objects[("egressbindings", "live-expiring")] = egress_binding(
        "live-expiring",
        subjects=[LIVE.model_dump()],
        policies=["pypi", "vanished"],
        from_git=False,
        expires_at="2026-12-01T00:00:00Z",
    )
    custom_objects.objects[("egressbindings", "live-granted")] = egress_binding(
        "live-granted", subjects=[LIVE.model_dump()], policies=["pypi"], from_git=False
    )
    custom_objects.objects[("egressbindings", "other-only")] = egress_binding(
        "other-only", subjects=[{"namespace": NAMESPACE, "name": "other"}], policies=["pypi"]
    )


async def test_bindings_for_lists_the_bindings_naming_the_subject(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    assert [view.name for view in await egress.bindings_for(LIVE)] == ["live-expiring", "live-granted", "live-seeded"]
    assert [view.name for view in await egress.bindings_for(OTHER)] == ["other-only"]


async def test_a_like_named_account_in_another_namespace_is_a_different_subject(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """A subject is a namespace and a name together: an account called "live" somewhere else is not
    this sandbox, and a binding naming both is still this sandbox's."""
    _seed(custom_objects)
    custom_objects.objects[("egressbindings", "elsewhere-only")] = egress_binding(
        "elsewhere-only", subjects=[{"namespace": "agentplane-elsewhere", "name": "live"}], policies=["pypi"]
    )
    custom_objects.objects[("egressbindings", "live-and-another")] = egress_binding(
        "live-and-another",
        subjects=[LIVE.model_dump(), {"namespace": NAMESPACE, "name": "test-workload-sa"}],
        policies=["pypi"],
    )

    names = [view.name for view in await egress.bindings_for(LIVE)]
    assert "elsewhere-only" not in names
    assert "live-and-another" in names

    both = next(view for view in await egress.bindings_for(LIVE) if view.name == "live-and-another")
    assert both.subjects == [LIVE, ServiceAccountRef(namespace=NAMESPACE, name="test-workload-sa")]


async def test_a_binding_view_carries_provenance_expiry_and_policies(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

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


async def test_revoke_deletes_a_runtime_binding_and_refuses_one_from_git(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Deleting the rule is the whole revocation; a Flux-applied one would come straight back, so
    the app refuses it instead of deleting an object the next reconcile re-creates."""
    _seed(custom_objects)

    await egress.revoke("live-granted")

    assert ("egressbindings", "live-granted") not in custom_objects.objects
    with pytest.raises(FluxOwnedBindingError):
        await egress.revoke("live-seeded")
    assert ("egressbindings", "live-seeded") in custom_objects.objects
    with pytest.raises(BindingNotFoundError):
        await egress.revoke("live-granted")


async def test_grant_creates_a_binding_the_sandbox_owns(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Creating the binding is the grant: nothing has to answer it afterwards. The API server names
    it, so the app learns the name from what it gets back."""
    _seed(custom_objects)
    granted = await egress.grant(LIVE_SANDBOX, ["pypi", "github"])

    assert granted.name.startswith("live-")
    created = custom_objects.objects[("egressbindings", granted.name)]
    (owner,) = created["metadata"]["ownerReferences"]
    assert owner == {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "name": "live",
        "uid": str(LIVE_SANDBOX.uid),
        "controller": False,
        "blockOwnerDeletion": False,
    }
    assert created["spec"] == {"subjects": [LIVE.model_dump()], "policies": ["pypi", "github"]}
    # And the app reads its own grant back like any other binding, not from git.
    (read_back,) = [view for view in await egress.bindings_for(LIVE) if view.name == granted.name]
    assert (read_back.from_git, [policy.name for policy in read_back.policies]) == (False, ["pypi", "github"])


async def test_a_second_grant_is_another_binding_and_not_an_edit_of_the_first(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """`expiresAt` is per binding, so granting a running sandbox cannot append to a binding that
    carries other policies: at the deadline it would take those away too."""
    _seed(custom_objects)
    first = await egress.grant(LIVE_SANDBOX, ["pypi"])
    second = await egress.grant(LIVE_SANDBOX, ["github"])

    assert first.name != second.name
    assert custom_objects.objects[("egressbindings", first.name)]["spec"]["policies"] == ["pypi"]
    assert custom_objects.objects[("egressbindings", second.name)]["spec"]["policies"] == ["github"]
    # Both name the sandbox, so the proxy's union over its bindings is what composes them.
    assert {first.name, second.name} <= {view.name for view in await egress.bindings_for(LIVE)}


async def test_a_grant_naming_a_policy_the_namespace_lacks_writes_nothing(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """A binding may carry a name nothing answers to — the proxy reports that as `MissingPolicy` —
    but the app will not mint one, so a mistyped grant leaves no rule behind to puzzle over."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    with pytest.raises(UnknownPolicyError) as refused:
        await egress.grant(LIVE_SANDBOX, ["pypi", "vanished"])

    assert refused.value.names == ["vanished"]
    assert set(custom_objects.objects) == before
    with pytest.raises(UnknownPolicyError):
        await egress.require_policies(["vanished"])


async def test_list_policies_summarises_every_rule(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

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
    custom_objects.objects[("egresspolicies", "workload")] = egress_policy(
        "workload", [{"hosts": ["agentplane.internal"], "credentialRef": {"name": "workload"}}]
    )

    credential = (await egress.list_policies())[0].rules[0].credential

    assert credential is not None
    assert (credential.name, credential.secret, credential.key) == ("workload", None, None)


async def test_a_rule_naming_a_credential_the_namespace_does_not_hold_says_which_name_dangles(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """The proxy substitutes nothing for it, so the page must not read as a rule that wanted none."""
    custom_objects.objects[("egresspolicies", "orphan")] = egress_policy(
        "orphan", [{"hosts": ["api.github.com"], "credentialRef": {"name": "gone"}}]
    )

    (rule,) = {policy.name: policy for policy in await egress.list_policies()}["orphan"].rules

    assert rule.credential is None
    assert rule.missing_credential == "gone"


async def test_operator_json_target_has_no_nullable_header_or_scheme(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)
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


@pytest.mark.parametrize(
    "target",
    [
        {"method": "jsonField", "field": "password", "header": "Authorization"},
        {"method": "jsonField", "field": "password", "scheme": None},
        {"method": "jsonField"},
        {"method": "wholeValue", "header": "X-Key", "field": "password"},
        {"method": "schemeToken", "header": "Authorization"},
    ],
)
def test_operator_api_rejects_mixed_target_variants(target: dict[str, str | None]) -> None:
    with pytest.raises(ValidationError):
        CredentialView.model_validate(
            {"name": "test", "description": "test credential", "placeholder": "test-placeholder", "targets": [target]}
        )


def test_operator_schema_preserves_the_target_discriminator() -> None:
    items = CredentialView.model_json_schema()["properties"]["targets"]["items"]
    assert items["discriminator"]["propertyName"] == "method"
    assert set(items["discriminator"]["mapping"]) == {method.value for method in TargetMethod}
    assert len(items["oneOf"]) == len(TargetMethod)


if __name__ == "__main__":
    pytest_bazel.main()
