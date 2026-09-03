"""Which bindings the app shows a sandbox, what it reads off them, and what it writes back."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel

from x.agentplane.app.conftest import APPROVER
from x.agentplane.app.egress import (
    GRANTED_BY_LABEL,
    ApprovalState,
    BindingNotFoundError,
    EgressInventory,
    FluxOwnedBindingError,
)
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
APPROVED = {"state": "approved", "by": "test-operator", "at": "2026-09-01T12:00:00Z"}


def _seed(custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[("egresspolicies", "github")] = egress_policy("github", [GITHUB_RULE])
    custom_objects.objects[("egresspolicies", "pypi")] = egress_policy("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[("egressbindings", "all-managed")] = egress_binding(
        "all-managed",
        subjects=[{"sandboxSelector": {"matchLabels": {MANAGED_LABEL: "true"}}}],
        policies=["github"],
        approval=APPROVED,
        active=("True", "Resolved", "1 of 1 policies resolved"),
    )
    custom_objects.objects[("egressbindings", "coders")] = egress_binding(
        "coders",
        subjects=[{"sandboxSelector": {"matchLabels": {PROFILE_LABEL: "coder"}}}],
        policies=["pypi", "vanished"],
        approval=APPROVED,
        expires_at="2026-12-01T00:00:00Z",
    )
    custom_objects.objects[("egressbindings", "live-asks")] = egress_binding(
        "live-asks",
        subjects=[{"sandbox": {"name": "live"}}],
        policies=["pypi"],
        approval={"state": "pending"},
        granted_by="agent",
        active=("False", "NotApproved", "approval is pending"),
    )
    custom_objects.objects[("egressbindings", "other-only")] = egress_binding(
        "other-only", subjects=[{"sandbox": {"name": "other"}}], policies=["pypi"], approval=APPROVED
    )


async def test_bindings_for_matches_by_name_and_by_every_selector_label(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    plain = await egress.bindings_for("live", {MANAGED_LABEL: "true"})
    coder = await egress.bindings_for("live", {MANAGED_LABEL: "true", PROFILE_LABEL: "coder"})
    unmanaged = await egress.bindings_for("other", {})

    assert [view.name for view in plain] == ["all-managed", "live-asks"]
    assert [view.name for view in coder] == ["all-managed", "coders", "live-asks"]
    assert [view.name for view in unmanaged] == ["other-only"]


async def test_a_binding_view_carries_provenance_approval_expiry_policies_and_the_proxy_condition(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    by_name = {
        view.name: view for view in await egress.bindings_for("live", {MANAGED_LABEL: "true", PROFILE_LABEL: "coder"})
    }

    seed = by_name["all-managed"]
    assert (seed.granted_by, seed.from_git, seed.approval, seed.approved_by) == (
        "flux",
        True,
        ApprovalState.APPROVED,
        "test-operator",
    )
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

    ask = by_name["live-asks"]
    assert (ask.granted_by, ask.from_git, ask.approval, ask.active, ask.active_reason) == (
        "agent",
        False,
        ApprovalState.PENDING,
        False,
        "NotApproved",
    )
    assert ask.subjects[0].sandbox == "live"


async def test_approve_and_deny_write_the_approval_as_the_deciding_operator(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    await egress.approve("live-asks", by=APPROVER)
    approved = dict(custom_objects.objects[("egressbindings", "live-asks")]["spec"]["approval"])
    await egress.deny("live-asks", by=APPROVER)
    denied = dict(custom_objects.objects[("egressbindings", "live-asks")]["spec"]["approval"])

    assert (approved["state"], approved["by"]) == ("approved", APPROVER)
    assert (denied["state"], denied["by"]) == ("denied", APPROVER)
    assert approved["at"].endswith("Z")
    assert denied["at"] >= approved["at"]
    with pytest.raises(BindingNotFoundError):
        await egress.approve("nope", by=APPROVER)


async def test_revoke_deletes_a_runtime_binding_and_refuses_one_from_git(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    await egress.revoke("live-asks")

    assert ("egressbindings", "live-asks") not in custom_objects.objects
    with pytest.raises(FluxOwnedBindingError):
        await egress.revoke("all-managed")
    assert ("egressbindings", "all-managed") in custom_objects.objects
    with pytest.raises(BindingNotFoundError):
        await egress.revoke("live-asks")


async def test_grant_creates_an_approved_binding_the_sandbox_owns(
    egress: EgressInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)
    uid = uuid4()

    await egress.grant(sandbox="live", sandbox_uid=uid, policies=["pypi", "github"], by=APPROVER)

    created = custom_objects.objects[("egressbindings", "live-picked")]
    assert created["metadata"]["labels"] == {GRANTED_BY_LABEL: APPROVER}
    (owner,) = created["metadata"]["ownerReferences"]
    assert owner == {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "name": "live",
        "uid": str(uid),
        "controller": False,
        "blockOwnerDeletion": False,
    }
    assert created["spec"]["subjects"] == [{"sandbox": {"name": "live"}}]
    assert created["spec"]["policies"] == ["pypi", "github"]
    assert (created["spec"]["approval"]["state"], created["spec"]["approval"]["by"]) == ("approved", APPROVER)
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
