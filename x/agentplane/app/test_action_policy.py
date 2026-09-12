"""What the app writes for a new Sandbox, and how it reads the Action Service's answer back."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import pytest_bazel

from x.agentplane.action_service.client import CredentialPlaceholder, OperatorActionServiceClient
from x.agentplane.action_service.models import PolicyKind
from x.agentplane.action_service.policy_view import (
    ActionPolicySetView,
    EffectivePolicyView,
    ExactActionsView,
    ReadyConditionView,
    SubjectActionPolicyView,
    SubjectBindingView,
)
from x.agentplane.app.action_policy import (
    MANAGED_BY_APP,
    MANAGED_BY_LABEL,
    ActionPolicyInventory,
    BindingProvenance,
    UnknownPolicySetError,
)
from x.agentplane.app.egress import FLUX_KUSTOMIZATION_LABEL
from x.agentplane.app.testing.kubernetes import NAMESPACE, FakeCustomObjectsApi, action_policy_set

LIVE_UID = UUID("11111111-0000-4000-8000-000000000001")
READS = {"type": "exact_actions", "actions": {"github": ["search_code", "get_file_contents"]}}


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi) -> ActionPolicyInventory:
    return ActionPolicyInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects))


def _seed(custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[("actionpolicysets", "reads")] = action_policy_set(
        "reads", auto_approve_if=[READS], ready=("True", "Valid", "spec accepted")
    )
    custom_objects.objects[("actionpolicysets", "issues")] = action_policy_set("issues", auto_approve_if=[READS])
    custom_objects.objects[("actionpolicysets", "broken")] = action_policy_set(
        "broken",
        auto_approve_if=[{"type": "no_such_kind", "actions": {"github": ["search_code"]}}],
        ready=("False", "Invalid", "spec.autoApproveIf.0: unknown kind"),
    )


async def test_bind_creates_a_labelled_binding_the_sandbox_owns(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """Creating the binding is the whole grant. The API server names it, the app's label says who
    wrote it, and the owner reference lets the Sandbox's deletion collect it."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    await inventory.bind(sandbox="live", sandbox_uid=LIVE_UID, policy_sets=["reads", "issues"])

    ((kind, name),) = set(custom_objects.objects) - before
    assert (kind, name.startswith("live-")) == ("actionpolicybindings", True)
    created = custom_objects.objects[(kind, name)]
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


async def test_a_binding_naming_a_set_the_namespace_lacks_writes_nothing(
    inventory: ActionPolicyInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    """The Action Service would answer the dangling name with nothing; the app refuses to mint it.
    A refused set still exists, so binding to it is the operator's call, not a typo."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    with pytest.raises(UnknownPolicySetError) as refused:
        await inventory.bind(sandbox="live", sandbox_uid=LIVE_UID, policy_sets=["reads", "vanished"])

    assert refused.value.names == ["vanished"]
    assert set(custom_objects.objects) == before
    with pytest.raises(UnknownPolicySetError):
        await inventory.require_policy_sets(["vanished"])
    await inventory.require_policy_sets(["reads", "broken"])


READY = ReadyConditionView(status="True", reason="Valid", message="spec accepted", observed_generation=1)
ANSWER = SubjectActionPolicyView(
    synced=True,
    bindings=[
        SubjectBindingView(
            name="live-afternoon",
            labels={},
            expires_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
            ready=None,
            policy_sets=[ActionPolicySetView(name="issues", generation=1)],
            missing_policy_sets=["vanished"],
        ),
        SubjectBindingView(
            name="live-k2m9x",
            labels={MANAGED_BY_LABEL: MANAGED_BY_APP},
            ready=READY,
            policy_sets=[ActionPolicySetView(name="reads", generation=3, ready=READY)],
            missing_policy_sets=[],
        ),
        SubjectBindingView(
            name="live-seeded",
            labels={FLUX_KUSTOMIZATION_LABEL: "test-actions", MANAGED_BY_LABEL: "someone-else"},
            ready=READY,
            policy_sets=[ActionPolicySetView(name="reads", generation=3, ready=READY)],
            missing_policy_sets=[],
        ),
    ],
    auto_approve_if=[
        EffectivePolicyView(
            binding="live-k2m9x",
            policy_set="reads",
            index=0,
            policy=ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions={"github": ["search_code"]}),
        )
    ],
    auto_deny_if=[],
    auto_deny_unless=[],
)


async def test_for_sandbox_asks_the_service_for_the_uid_and_adds_only_who_wrote_each_binding(
    inventory: ActionPolicyInventory,
) -> None:
    """The route and the live frame both go through here: the service's answer for the Sandbox's
    UID in the sandbox namespace, with labels read into provenance and nothing else re-derived."""
    asked: list[str] = []

    def service(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json=ANSWER.model_dump(mode="json"))

    client = OperatorActionServiceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(service), base_url="http://test-actions.invalid"),
        CredentialPlaceholder("test-operator-token"),
    )

    view = await inventory.for_sandbox(client, LIVE_UID)

    assert asked == [f"/v1/operator/action-policy/sandboxes/{NAMESPACE}/{LIVE_UID}"]
    assert [(b.name, b.provenance) for b in view.bindings] == [
        ("live-afternoon", BindingProvenance.OPERATOR),
        ("live-k2m9x", BindingProvenance.APP),
        ("live-seeded", BindingProvenance.GIT),
    ]
    afternoon, app_written, _ = view.bindings
    assert (afternoon.expires_at, afternoon.ready, afternoon.missing_policy_sets) == (
        datetime(2026, 9, 12, 20, tzinfo=UTC),
        None,
        ["vanished"],
    )
    assert app_written.policy_sets == ANSWER.bindings[1].policy_sets
    assert (view.synced, view.auto_approve_if, view.auto_deny_if) == (True, ANSWER.auto_approve_if, [])


if __name__ == "__main__":
    pytest_bazel.main()
