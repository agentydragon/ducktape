"""What the app writes for a new Sandbox, and what it shows of the objects the Action Service reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from x.agentplane.action_service.models import PolicyKind
from x.agentplane.app.action_policy import (
    MANAGED_BY_APP,
    MANAGED_BY_LABEL,
    ActionPolicyInventory,
    ActionPolicyView,
    ArgumentSchemaView,
    BindingProvenance,
    ExactActionsView,
    UnknownPolicySetError,
)
from x.agentplane.app.testing.kubernetes import (
    NAMESPACE,
    FakeCustomObjectsApi,
    action_policy_binding,
    action_policy_set,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
LIVE_UID = UUID("11111111-0000-4000-8000-000000000001")
READS = {"type": "exact_actions", "actions": {"github": ["search_code", "get_file_contents"]}}
ISSUE_SCHEMA: dict[str, Any] = {"properties": {"owner": {"const": "test-owner"}}, "required": ["owner"]}
ISSUES = {"type": "argument_schema", "actions": {"github": ["create_issue"]}, "schema": ISSUE_SCHEMA}


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi) -> ActionPolicyInventory:
    return ActionPolicyInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects), clock=lambda: NOW)


def _seed(custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[("actionpolicysets", "reads")] = action_policy_set(
        "reads", auto_approve_if=[READS], generation=3, ready=("True", "Valid", "spec accepted"), observed_generation=2
    )
    custom_objects.objects[("actionpolicysets", "issues")] = action_policy_set(
        "issues", auto_approve_if=[ISSUES], auto_deny_if=[READS], ready=("True", "Valid", "spec accepted")
    )
    custom_objects.objects[("actionpolicysets", "broken")] = action_policy_set(
        "broken",
        auto_approve_if=[{"type": "no_such_kind", "actions": {"github": ["search_code"]}}],
        ready=("False", "Invalid", "spec.autoApproveIf.0: unknown kind"),
    )
    custom_objects.objects[("actionpolicybindings", "live-seeded")] = action_policy_binding(
        "live-seeded",
        subject={"sandbox": {"name": "live", "uid": str(LIVE_UID)}},
        policy_sets=["reads"],
        ready=("True", "Valid", "spec accepted"),
    )
    custom_objects.objects[("actionpolicybindings", "live-afternoon")] = action_policy_binding(
        "live-afternoon",
        subject={"sandbox": {"name": "live", "uid": str(LIVE_UID)}},
        policy_sets=["issues", "vanished", "broken"],
        from_git=False,
        expires_at="2026-09-12T20:00:00Z",
    )
    custom_objects.objects[("actionpolicybindings", "live-lapsed")] = action_policy_binding(
        "live-lapsed",
        subject={"sandbox": {"name": "live", "uid": str(LIVE_UID)}},
        policy_sets=["issues"],
        from_git=False,
        expires_at="2026-09-12T11:59:00Z",
    )
    # The same name, another incarnation: the UID is what a subject pins.
    custom_objects.objects[("actionpolicybindings", "live-previous")] = action_policy_binding(
        "live-previous", subject={"sandbox": {"name": "live", "uid": str(uuid4())}}, policy_sets=["reads"]
    )
    custom_objects.objects[("actionpolicybindings", "external")] = action_policy_binding(
        "external", subject={"serviceAccount": {"namespace": NAMESPACE, "name": "test-client"}}, policy_sets=["reads"]
    )


async def test_for_sandbox_selects_the_unexpired_bindings_pinning_the_uid(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """A lapsed binding, one naming a previous Sandbox of the same name, and a ServiceAccount's
    are what the Action Service would skip, so they are not shown as what the Sandbox can do."""
    _seed(custom_objects)

    view = await inventory.for_sandbox(LIVE_UID)

    assert [binding.name for binding in view.bindings] == ["live-afternoon", "live-seeded"]
    assert (await inventory.for_sandbox(uuid4())).bindings == []


async def test_a_binding_view_carries_provenance_expiry_readiness_and_its_sets(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    _seed(custom_objects)

    by_name = {binding.name: binding for binding in (await inventory.for_sandbox(LIVE_UID)).bindings}

    seeded = by_name["live-seeded"]
    assert (seeded.provenance, seeded.expires_at) == (BindingProvenance.GIT, None)
    assert seeded.ready is not None
    assert (seeded.ready.status, seeded.ready.reason) == ("True", "Valid")
    (reads,) = seeded.policy_sets
    # Judged at generation 2, edited since: the page can say the service has not seen the edit.
    assert reads.ready is not None
    assert (reads.generation, reads.ready.observed_generation, reads.refused) == (3, 2, None)

    afternoon = by_name["live-afternoon"]
    assert (afternoon.provenance, afternoon.expires_at, afternoon.ready) == (
        BindingProvenance.OPERATOR,
        datetime(2026, 9, 12, 20, tzinfo=UTC),
        None,
    )
    assert [policy_set.name for policy_set in afternoon.policy_sets] == ["issues", "broken"]
    assert afternoon.missing_policy_sets == ["vanished"]
    broken = afternoon.policy_sets[1]
    assert broken.refused is not None
    assert "no_such_kind" in broken.refused
    assert broken.ready is not None
    assert broken.ready.status == "False"


async def test_the_effective_lists_walk_bindings_by_name_then_sets_then_policies(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """The order the Action Service evaluates in, with a refused or missing set contributing
    nothing; a policy names the binding, set and index a Decision's evidence would."""
    _seed(custom_objects)

    view = await inventory.for_sandbox(LIVE_UID)

    assert [(p.binding, p.policy_set, p.index, p.policy.type) for p in view.auto_approve_if] == [
        ("live-afternoon", "issues", 0, PolicyKind.ARGUMENT_SCHEMA),
        ("live-seeded", "reads", 0, PolicyKind.EXACT_ACTIONS),
    ]
    issue, read = (p.policy for p in view.auto_approve_if)
    assert isinstance(issue, ArgumentSchemaView)
    assert (issue.actions, issue.argument_schema) == ({"github": ["create_issue"]}, ISSUE_SCHEMA)
    assert isinstance(read, ExactActionsView)
    assert read.actions == {"github": ["get_file_contents", "search_code"]}
    assert [(p.binding, p.policy_set) for p in view.auto_deny_if] == [("live-afternoon", "issues")]
    assert view.auto_deny_unless == []


async def test_bind_creates_a_labelled_binding_the_sandbox_owns(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Creating the binding is the whole grant. The API server names it, the app's label says who
    wrote it, and the owner reference lets the Sandbox's deletion collect it."""
    _seed(custom_objects)

    bound = await inventory.bind(sandbox="live", sandbox_uid=LIVE_UID, policy_sets=["reads", "issues"])

    assert bound.name.startswith("live-")
    assert bound.provenance is BindingProvenance.APP
    assert [policy_set.name for policy_set in bound.policy_sets] == ["reads", "issues"]
    created = custom_objects.objects[("actionpolicybindings", bound.name)]
    assert created["metadata"]["labels"] == {MANAGED_BY_LABEL: MANAGED_BY_APP}
    (owner,) = created["metadata"]["ownerReferences"]
    assert owner == {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "name": "live",
        "uid": str(LIVE_UID),
        "controller": False,
        "blockOwnerDeletion": False,
    }
    assert created["spec"] == {
        "subject": {"sandbox": {"name": "live", "uid": str(LIVE_UID)}},
        "policySets": ["reads", "issues"],
    }
    # And the app reads its own binding back like any other.
    view: ActionPolicyView = await inventory.for_sandbox(LIVE_UID)
    assert bound.name in {binding.name for binding in view.bindings}


async def test_a_binding_naming_a_set_the_namespace_lacks_writes_nothing(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """The Action Service would answer the dangling name with nothing; the app refuses to mint it."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    with pytest.raises(UnknownPolicySetError) as refused:
        await inventory.bind(sandbox="live", sandbox_uid=LIVE_UID, policy_sets=["reads", "vanished"])

    assert refused.value.names == ["vanished"]
    assert set(custom_objects.objects) == before
    with pytest.raises(UnknownPolicySetError):
        await inventory.require_policy_sets(["vanished"])
    await inventory.require_policy_sets(["reads", "broken"])


if __name__ == "__main__":
    pytest_bazel.main()
