"""Which bindings the app shows a sandbox, what it reads off them, and what it writes back."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel

from x.agentplane.app.conftest import GRANTER
from x.agentplane.app.egress import GRANTED_BY_LABEL, BindingNotFoundError, EgressInventory, FluxOwnedBindingError
from x.agentplane.app.inventory import MANAGED_LABEL, PROFILE_LABEL
from x.agentplane.app.testing.kubernetes import FakeCustomObjectsApi, egress_binding, egress_policy

GITHUB_RULE = {
    "hosts": ["api.github.com", "*.githubusercontent.com"],
    "methods": ["GET", "POST"],
    "credential": {
        "secretRef": {"name": "test-github-pat", "key": "token"},
        "header": "Authorization",
        "placeholder": "test-placeholder",
    },
}


def _seed(custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[("egresspolicies", "github")] = egress_policy("github", [GITHUB_RULE])
    custom_objects.objects[("egresspolicies", "pypi")] = egress_policy("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[("egressbindings", "all-managed")] = egress_binding(
        "all-managed",
        subjects=[{"sandboxSelector": {"matchLabels": {MANAGED_LABEL: "true"}}}],
        policies=["github"],
        active=("True", "Resolved", "1 of 1 policies resolved"),
    )
    custom_objects.objects[("egressbindings", "coders")] = egress_binding(
        "coders",
        subjects=[{"sandboxSelector": {"matchLabels": {PROFILE_LABEL: "coder"}}}],
        policies=["pypi", "vanished"],
        expires_at="2026-12-01T00:00:00Z",
    )
    custom_objects.objects[("egressbindings", "live-granted")] = egress_binding(
        "live-granted",
        subjects=[{"sandbox": {"name": "live"}}],
        policies=["pypi"],
        granted_by="agent",
        active=("False", "Expired", "1 of 1 policies resolved"),
    )
    custom_objects.objects[("egressbindings", "other-only")] = egress_binding(
        "other-only", subjects=[{"sandbox": {"name": "other"}}], policies=["pypi"]
    )


async def test_bindings_for_matches_by_name_and_by_every_selector_label(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    plain = await egress.bindings_for("live", {MANAGED_LABEL: "true"})
    coder = await egress.bindings_for("live", {MANAGED_LABEL: "true", PROFILE_LABEL: "coder"})
    unmanaged = await egress.bindings_for("other", {})

    assert [view.name for view in plain] == ["all-managed", "live-granted"]
    assert [view.name for view in coder] == ["all-managed", "coders", "live-granted"]
    assert [view.name for view in unmanaged] == ["other-only"]


async def test_a_binding_view_carries_provenance_expiry_policies_and_the_proxy_condition(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    by_name = {
        view.name: view for view in await egress.bindings_for("live", {MANAGED_LABEL: "true", PROFILE_LABEL: "coder"})
    }

    seed = by_name["all-managed"]
    assert (seed.granted_by, seed.from_git) == ("flux", True)
    assert (seed.active, seed.active_reason, seed.active_message) == (True, "Resolved", "1 of 1 policies resolved")
    assert seed.subjects[0].match_labels == {MANAGED_LABEL: "true"}
    (policy,) = seed.policies
    (rule,) = policy.rules
    assert (policy.name, rule.hosts, rule.methods, rule.paths) == (
        "github",
        ["api.github.com", "*.githubusercontent.com"],
        ["GET", "POST"],
        None,
    )
    assert rule.credential is not None
    assert (rule.credential.secret, rule.credential.key, rule.credential.header) == (
        "test-github-pat",
        "token",
        "Authorization",
    )

    coders = by_name["coders"]
    assert coders.expires_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert ([policy.name for policy in coders.policies], coders.missing_policies) == (["pypi"], ["vanished"])
    assert coders.active is None

    granted = by_name["live-granted"]
    assert (granted.granted_by, granted.from_git, granted.active, granted.active_reason) == (
        "agent",
        False,
        False,
        "Expired",
    )
    assert granted.subjects[0].sandbox == "live"


async def test_revoke_deletes_a_runtime_binding_and_refuses_one_from_git(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Deleting the rule is the whole revocation; a Flux-applied one would come straight back, so
    the app refuses it instead of deleting an object the next reconcile re-creates."""
    _seed(custom_objects)

    await egress.revoke("live-granted")

    assert ("egressbindings", "live-granted") not in custom_objects.objects
    with pytest.raises(FluxOwnedBindingError):
        await egress.revoke("all-managed")
    assert ("egressbindings", "all-managed") in custom_objects.objects
    with pytest.raises(BindingNotFoundError):
        await egress.revoke("live-granted")


async def test_grant_creates_a_binding_the_sandbox_owns(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Creating the binding is the grant: it carries who made it, and nothing has to answer it."""
    _seed(custom_objects)
    uid = uuid4()

    await egress.grant(sandbox="live", sandbox_uid=uid, policies=["pypi", "github"], by=GRANTER)

    created = custom_objects.objects[("egressbindings", "live-picked")]
    assert created["metadata"]["labels"] == {GRANTED_BY_LABEL: GRANTER.label}
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
    (picked,) = [view for view in await egress.bindings_for("live", {}) if view.name == "live-picked"]
    assert (picked.from_git, [policy.name for policy in picked.policies]) == (False, ["pypi", "github"])


async def test_list_policies_summarises_every_rule(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    policies = {policy.name: policy for policy in await egress.list_policies()}

    assert set(policies) == {"github", "pypi"}
    (open_rule,) = policies["pypi"].rules
    assert (open_rule.hosts, open_rule.methods, open_rule.credential) == (["pypi.org"], None, None)


if __name__ == "__main__":
    pytest_bazel.main()
