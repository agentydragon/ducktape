"""Which bindings the app shows a sandbox, what it reads off them, and what it writes back."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel

from x.agentplane.app.egress import BindingNotFoundError, EgressInventory, FluxOwnedBindingError, UnknownPolicyError
from x.agentplane.app.testing.kubernetes import FakeCustomObjectsApi, egress_binding, egress_credential, egress_policy

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
        "live-seeded",
        subjects=[{"sandbox": {"name": "live"}}],
        policies=["github"],
        active=("True", "Resolved", "1 of 1 policies resolved"),
    )
    custom_objects.objects[("egressbindings", "live-expiring")] = egress_binding(
        "live-expiring",
        subjects=[{"sandbox": {"name": "live"}}],
        policies=["pypi", "vanished"],
        from_git=False,
        expires_at="2026-12-01T00:00:00Z",
    )
    custom_objects.objects[("egressbindings", "live-granted")] = egress_binding(
        "live-granted",
        subjects=[{"sandbox": {"name": "live"}}],
        policies=["pypi"],
        from_git=False,
        active=("False", "Expired", "1 of 1 policies resolved"),
    )
    custom_objects.objects[("egressbindings", "other-only")] = egress_binding(
        "other-only", subjects=[{"sandbox": {"name": "other"}}], policies=["pypi"]
    )


async def test_bindings_for_lists_the_bindings_naming_the_sandbox(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    assert [view.name for view in await egress.bindings_for("live")] == ["live-expiring", "live-granted", "live-seeded"]
    assert [view.name for view in await egress.bindings_for("other")] == ["other-only"]


async def test_a_binding_view_carries_provenance_expiry_policies_and_the_proxy_condition(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    by_name = {view.name: view for view in await egress.bindings_for("live")}

    seed = by_name["live-seeded"]
    assert seed.from_git
    assert (seed.active, seed.active_reason, seed.active_message) == (True, "Resolved", "1 of 1 policies resolved")
    assert seed.subjects == ["live"]
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
    assert [(t.header, t.method, t.scheme) for t in rule.credential.targets] == [
        ("Authorization", "schemeToken", "Bearer")
    ]
    assert rule.missing_credential is None

    expiring = by_name["live-expiring"]
    assert expiring.expires_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert ([policy.name for policy in expiring.policies], expiring.missing_policies) == (["pypi"], ["vanished"])
    assert expiring.active is None

    granted = by_name["live-granted"]
    assert (granted.from_git, granted.active, granted.active_reason) == (False, False, "Expired")
    assert granted.subjects == ["live"]


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
    uid = uuid4()

    granted = await egress.grant(sandbox="live", sandbox_uid=uid, policies=["pypi", "github"])

    assert granted.name.startswith("live-")
    created = custom_objects.objects[("egressbindings", granted.name)]
    (owner,) = created["metadata"]["ownerReferences"]
    assert owner == {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "name": "live",
        "uid": str(uid),
        "controller": False,
        "blockOwnerDeletion": False,
    }
    assert created["spec"] == {"subjects": [{"sandbox": {"name": "live"}}], "policies": ["pypi", "github"]}
    # And the app reads its own grant back like any other binding, not from git.
    (read_back,) = [view for view in await egress.bindings_for("live") if view.name == granted.name]
    assert (read_back.from_git, [policy.name for policy in read_back.policies]) == (False, ["pypi", "github"])


async def test_a_second_grant_is_another_binding_and_not_an_edit_of_the_first(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """`expiresAt` is per binding, so granting a running sandbox cannot append to a binding that
    carries other policies: at the deadline it would take those away too."""
    _seed(custom_objects)
    uid = uuid4()

    first = await egress.grant(sandbox="live", sandbox_uid=uid, policies=["pypi"])
    second = await egress.grant(sandbox="live", sandbox_uid=uid, policies=["github"])

    assert first.name != second.name
    assert custom_objects.objects[("egressbindings", first.name)]["spec"]["policies"] == ["pypi"]
    assert custom_objects.objects[("egressbindings", second.name)]["spec"]["policies"] == ["github"]
    assert custom_objects.patches == []
    # Both name the sandbox, so the proxy's union over its bindings is what composes them.
    assert {first.name, second.name} <= {view.name for view in await egress.bindings_for("live")}


async def test_a_grant_naming_a_policy_the_namespace_lacks_writes_nothing(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """A binding may carry a name nothing answers to — the proxy reports that as `MissingPolicy` —
    but the app will not mint one, so a mistyped grant leaves no rule behind to puzzle over."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    with pytest.raises(UnknownPolicyError) as refused:
        await egress.grant(sandbox="live", sandbox_uid=uuid4(), policies=["pypi", "vanished"])

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


if __name__ == "__main__":
    pytest_bazel.main()
