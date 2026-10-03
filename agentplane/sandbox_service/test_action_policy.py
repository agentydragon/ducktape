"""Service-owned policy binding mutation tests."""

from typing import cast

import pytest
import pytest_bazel

from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.action_policy_views import MANAGED_BY_APP, MANAGED_BY_LABEL, UnknownPolicySetError
from agentplane.sandbox_service.protocol_pb2 import Sandbox, ServiceAccount
from agentplane.sandbox_service.testing.fake_inventory import NAMESPACE, FakeCustomObjectsApi, action_policy_set
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf

LIVE = Sandbox(name="live", uid="test-live-uid", service_account=ServiceAccount(namespace=NAMESPACE, name="live"))
READS = {"type": "exact_actions", "actions": {"github": ["search_code", "get_file_contents"]}}


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi) -> ActionPolicyBindings:
    return ActionPolicyBindings(namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom_objects))


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
    inventory: ActionPolicyBindings, custom_objects: FakeCustomObjectsApi
) -> None:
    """Creating the binding is the whole grant. The API server names it, the app's label says who
    wrote it, and the owner reference lets the Sandbox's deletion collect it."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    await inventory.bind(LIVE, ["reads", "issues"])

    ((kind, name),) = set(custom_objects.objects) - before
    assert (kind, name.startswith("live-")) == ("actionpolicybindings", True)
    created = custom_objects.objects[(kind, name)]
    assert created["metadata"]["labels"] == {MANAGED_BY_LABEL: MANAGED_BY_APP}
    (owner,) = created["metadata"]["ownerReferences"]
    assert owner == {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "name": "live",
        "uid": str(LIVE.uid),
        "controller": False,
        "blockOwnerDeletion": False,
    }
    assert created["spec"] == {"subject": {"namespace": NAMESPACE, "name": "live"}, "policySets": ["reads", "issues"]}


async def test_a_binding_naming_a_set_the_namespace_lacks_writes_nothing(
    inventory: ActionPolicyBindings, custom_objects: FakeCustomObjectsApi
) -> None:
    """The Action Service would answer the dangling name with nothing; the app refuses to mint it.
    A refused set still exists, so binding to it is the operator's call, not a typo."""
    _seed(custom_objects)
    before = set(custom_objects.objects)

    with pytest.raises(UnknownPolicySetError) as refused:
        await inventory.bind(LIVE, ["reads", "vanished"])

    assert refused.value.names == ["vanished"]
    assert set(custom_objects.objects) == before
    with pytest.raises(UnknownPolicySetError):
        await inventory.require_policy_sets(["vanished"])
    await inventory.require_policy_sets(["reads", "broken"])


if __name__ == "__main__":
    pytest_bazel.main()
