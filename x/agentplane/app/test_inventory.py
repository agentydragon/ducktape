"""The inventory's reading of the Sandbox and Pod, and the objects each operation writes."""

from __future__ import annotations

import re

import pytest
import pytest_bazel

from x.agentplane.app.inventory import (
    ARCHIVED_LABEL,
    MANAGED_LABEL,
    MODEL_ANNOTATION,
    PROVIDER_LABEL,
    NewSandbox,
    Provider,
    ProvisioningState,
    SandboxInventory,
    SandboxNotFoundError,
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
    custom_objects.objects[("sandboxes", "live")] = sandbox(
        "live", provider=Provider.CODEX, model="cheap-codex", status=_READY
    )
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxes", "paused")] = sandbox("paused", operating_mode="Suspended")
    custom_objects.objects[("sandboxes", "shelved")] = sandbox(
        "shelved", labels={ARCHIVED_LABEL: "true"}, operating_mode="Suspended"
    )
    # Not Agentplane's: another tenant's Sandbox in the same namespace stays invisible.
    custom_objects.objects[("sandboxes", "foreign")] = {
        "metadata": {"name": "foreign", "creationTimestamp": "2026-09-01T12:00:00Z"},
        "spec": {"podTemplate": POD_TEMPLATE},
    }


async def test_list_derives_each_provisioning_state_from_the_sandbox_and_its_pod(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    views = {view.name: view for view in await inventory.list_sandboxes(include_archived=True)}

    assert {name: view.state for name, view in views.items()} == {
        "podless": ProvisioningState.WAITING_FOR_POD,
        "starting": ProvisioningState.WAITING_FOR_POD_READY,
        "live": ProvisioningState.RUNNING,
        "paused": ProvisioningState.SUSPENDED,
        "shelved": ProvisioningState.ARCHIVED,
    }
    live = views["live"]
    assert (live.provider, live.model) == (Provider.CODEX, "cheap-codex")
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
    assert views["shelved"].archived
    assert not views["live"].archived


async def test_list_hides_archived_sandboxes_unless_asked(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    names = {view.name for view in await inventory.list_sandboxes()}

    assert "shelved" not in names
    assert "live" in names


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
    view = await inventory.create(NewSandbox(slug="my-task", provider=Provider.CLAUDE, model="claude-cheap"))

    assert re.fullmatch(r"my-task-[a-z0-9]{5}", view.name)
    assert view.state == ProvisioningState.WAITING_FOR_POD
    stored = custom_objects.objects[("sandboxes", view.name)]
    assert stored["kind"] == "Sandbox"
    assert stored["metadata"]["labels"] == {MANAGED_LABEL: "true", PROVIDER_LABEL: "claude"}
    assert stored["metadata"]["annotations"] == {MODEL_ANNOTATION: "claude-cheap"}
    assert stored["spec"] == {
        "podTemplate": POD_TEMPLATE,
        "volumeClaimTemplates": VOLUME_CLAIM_TEMPLATES,
        "shutdownPolicy": "Retain",
    }


async def test_create_names_each_sandbox_uniquely(inventory: SandboxInventory) -> None:
    spec = NewSandbox(slug="twice", provider=Provider.CODEX, model="m")

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


async def test_archive_suspends_then_labels(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.archive("live")

    assert custom_objects.patches == [
        ("sandboxes", "live", {"spec": {"operatingMode": "Suspended"}}),
        ("sandboxes", "live", {"metadata": {"labels": {ARCHIVED_LABEL: "true"}}}),
    ]
    assert (await inventory.get("live")).state == ProvisioningState.ARCHIVED
    assert "live" not in {view.name for view in await inventory.list_sandboxes()}


async def test_unarchive_clears_the_label_and_leaves_the_sandbox_suspended(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.unarchive("shelved")

    assert custom_objects.patches == [("sandboxes", "shelved", {"metadata": {"labels": {ARCHIVED_LABEL: None}}})]
    assert ARCHIVED_LABEL not in custom_objects.objects[("sandboxes", "shelved")]["metadata"]["labels"]
    assert (await inventory.get("shelved")).state == ProvisioningState.SUSPENDED


async def test_delete_removes_the_sandbox(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    """The controller, not the app, takes the Pod and PVC down behind the Sandbox."""
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.delete("live")

    assert custom_objects.deleted == [("sandboxes", "live")]
    with pytest.raises(SandboxNotFoundError):
        await inventory.delete("live")


if __name__ == "__main__":
    pytest_bazel.main()
