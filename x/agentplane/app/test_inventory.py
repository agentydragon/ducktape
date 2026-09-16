"""The inventory's reading of the Sandbox and Pod, and the objects each operation writes."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from x.agentplane.app.inventory import (
    MANAGED_LABEL,
    NewSandbox,
    ProvisioningState,
    SandboxInventory,
    SandboxNotFoundError,
    SandboxRunningError,
)
from x.agentplane.app.testing.kubernetes import (
    POD_TEMPLATE,
    VOLUME_CLAIM_TEMPLATES,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    pod,
    sandbox,
)

_READY = {"conditions": [{"type": "Ready", "status": "True", "reason": "PodReady"}], "nodeName": "test-node"}


def _populate_one_of_each_state(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> None:
    custom_objects.objects[("sandboxes", "podless")] = sandbox("podless")
    custom_objects.objects[("sandboxes", "starting")] = sandbox("starting")
    core_v1.pods["starting"] = pod("starting", phase="Pending", ready=False, ip=None, waiting_reason="ImagePullBackOff")
    custom_objects.objects[("sandboxes", "live")] = sandbox("live", status=_READY)
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxes", "paused")] = sandbox("paused", operating_mode="Suspended")
    # Not Agentplane's: another tenant's Sandbox in the same namespace stays invisible.
    custom_objects.objects[("sandboxes", "foreign")] = {
        "metadata": {"name": "foreign", "uid": str(uuid4()), "creationTimestamp": "2026-09-01T12:00:00Z"},
        "spec": {"podTemplate": POD_TEMPLATE},
    }


async def test_list_derives_each_provisioning_state_from_the_sandbox_and_its_pod(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    views = {view.name: view for view in await inventory.list_sandboxes()}

    assert {name: view.state for name, view in views.items()} == {
        "podless": ProvisioningState.WAITING_FOR_POD,
        "starting": ProvisioningState.WAITING_FOR_POD_READY,
        "live": ProvisioningState.RUNNING,
        "paused": ProvisioningState.SUSPENDED,
    }
    live = views["live"]
    assert live.pod is not None
    assert (live.node_name, live.pod.phase, live.pod.ip, live.pod.node_name) == (
        "test-node",
        "Running",
        "10.0.0.7",
        "test-node",
    )
    assert [(condition.type, condition.status, condition.reason) for condition in live.conditions] == [
        ("Ready", "True", "PodReady")
    ]
    assert [(container.name, container.state, container.ready) for container in live.pod.containers] == [
        ("runner", "running", True)
    ]
    # A Pod held up by its image is visible as such, so the app can say why nothing is running.
    starting = views["starting"].pod
    assert starting is not None
    assert [(container.state, container.reason, container.message) for container in starting.containers] == [
        ("waiting", "ImagePullBackOff", "ImagePullBackOff on starting")
    ]
    assert (views["podless"].pod, views["podless"].conditions) == (None, [])


async def test_get_reads_one_sandbox_and_refuses_foreign_or_missing_ones(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    view = await inventory.get("live")

    assert view.pod is not None
    assert (view.state, view.pod.ip) == (ProvisioningState.RUNNING, "10.0.0.7")
    with pytest.raises(SandboxNotFoundError):
        await inventory.get("foreign")
    with pytest.raises(SandboxNotFoundError):
        await inventory.get("never-made")


async def test_create_stamps_a_labelled_sandbox_from_the_template(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    view = await inventory.create(NewSandbox(slug="my-task", template="agentplane-test-runner"))

    assert re.fullmatch(r"my-task-[a-z0-9]{5}", view.name)
    assert view.state == ProvisioningState.WAITING_FOR_POD
    stored = custom_objects.objects[("sandboxes", view.name)]
    assert stored["kind"] == "Sandbox"
    assert stored["metadata"]["labels"] == {MANAGED_LABEL: "true"}
    assert stored["spec"]["volumeClaimTemplates"] == VOLUME_CLAIM_TEMPLATES
    assert stored["spec"]["shutdownPolicy"] == "Retain"
    # Every other field of the Pod is the template's; only what it runs as is this sandbox's.
    assert stored["spec"]["podTemplate"] == {
        **POD_TEMPLATE,
        "spec": {**POD_TEMPLATE.get("spec", {}), "serviceAccountName": view.name},
    }


async def test_create_gives_the_sandbox_a_service_account_of_its_own_that_it_runs_as(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    """What the Pod runs as is what egress and the Action Service authenticate it by, so a sandbox
    sharing the template's account could only ever be granted what every other sandbox is."""
    view = await inventory.create(NewSandbox(slug="my-task", template="agentplane-test-runner"))

    account = core_v1.service_accounts[view.name]
    assert account.metadata.labels == {MANAGED_LABEL: "true"}
    assert (
        custom_objects.objects[("sandboxes", view.name)]["spec"]["podTemplate"]["spec"]["serviceAccountName"]
        == view.name
    )
    # Owned by the Sandbox, so deleting the sandbox takes the identity with it.
    (owner,) = account.metadata.owner_references
    assert (owner.kind, owner.name, owner.uid) == (
        "Sandbox",
        view.name,
        custom_objects.objects[("sandboxes", view.name)]["metadata"]["uid"],
    )


async def test_create_leaves_no_service_account_behind_when_the_sandbox_is_refused(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    """The account is written first, because a Pod naming one that does not exist is refused. If the
    Sandbox never appears, nothing owns the account and nothing would ever collect it."""
    custom_objects.create_fails = True

    with pytest.raises(k8s_client.ApiException):
        await inventory.create(NewSandbox(slug="my-task", template="agentplane-test-runner"))

    assert core_v1.service_accounts == {}


async def test_create_names_each_sandbox_uniquely(inventory: SandboxInventory) -> None:
    spec = NewSandbox(slug="twice", template="agentplane-test-runner")

    first, second = await inventory.create(spec), await inventory.create(spec)

    assert first.name != second.name


async def test_suspend_and_resume_patch_the_operating_mode(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.suspend("live")
    suspended = await inventory.get("live")
    await inventory.resume("live")
    resumed = await inventory.get("live")

    assert custom_objects.patches == [
        ("sandboxes", "live", {"spec": {"operatingMode": "Suspended"}}),
        ("sandboxes", "live", {"spec": {"operatingMode": "Running"}}),
    ]
    assert (suspended.state, resumed.state) == (ProvisioningState.SUSPENDED, ProvisioningState.RUNNING)
    with pytest.raises(SandboxNotFoundError):
        await inventory.suspend("foreign")


async def test_delete_takes_a_suspended_sandbox_and_refuses_a_running_one(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    """The controller, not the app, takes the Pod and PVC down behind the Sandbox."""
    _populate_one_of_each_state(custom_objects, core_v1)

    with pytest.raises(SandboxRunningError):
        await inventory.delete("live")
    assert custom_objects.deleted == []

    await inventory.suspend("live")
    await inventory.delete("live")

    assert custom_objects.deleted == [("sandboxes", "live")]
    with pytest.raises(SandboxNotFoundError):
        await inventory.delete("live")


if __name__ == "__main__":
    pytest_bazel.main()
