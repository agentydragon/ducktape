"""The inventory's reading of the CR graph and the patches each operation writes."""

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
    SandboxNotProvisionedError,
)
from x.agentplane.app.testing.kubernetes import FakeCoreV1Api, FakeCustomObjectsApi, claim, pod, sandbox

_READY = {"conditions": [{"type": "Ready", "status": "True"}]}


def _populate_one_of_each_state(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> None:
    custom_objects.objects[("sandboxclaims", "fresh")] = claim("fresh")
    custom_objects.objects[("sandboxclaims", "unassigned")] = claim(
        "unassigned", status={"conditions": [{"type": "Ready", "status": "False"}]}
    )
    custom_objects.objects[("sandboxclaims", "podless")] = claim(
        "podless", status={**_READY, "sandbox": {"name": "sb-podless"}}
    )
    custom_objects.objects[("sandboxes", "sb-podless")] = sandbox("sb-podless")
    custom_objects.objects[("sandboxclaims", "starting")] = claim(
        "starting", status={**_READY, "sandbox": {"name": "sb-starting"}}
    )
    custom_objects.objects[("sandboxes", "sb-starting")] = sandbox("sb-starting")
    core_v1.pods["sb-starting"] = pod("sb-starting", phase="Pending", ready=False, ip=None)
    custom_objects.objects[("sandboxclaims", "live")] = claim(
        "live", provider=Provider.CODEX, model="cheap-codex", status={**_READY, "sandbox": {"name": "sb-live"}}
    )
    custom_objects.objects[("sandboxes", "sb-live")] = sandbox("sb-live")
    core_v1.pods["sb-live"] = pod("sb-live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxclaims", "paused")] = claim(
        "paused", status={**_READY, "sandbox": {"name": "sb-paused"}}
    )
    custom_objects.objects[("sandboxes", "sb-paused")] = sandbox("sb-paused", operating_mode="Suspended")
    custom_objects.objects[("sandboxclaims", "shelved")] = claim(
        "shelved", labels={ARCHIVED_LABEL: "true"}, status={**_READY, "sandbox": {"name": "sb-shelved"}}
    )
    custom_objects.objects[("sandboxes", "sb-shelved")] = sandbox("sb-shelved", operating_mode="Suspended")
    # Not Agentplane's: another tenant's claim in the same namespace stays invisible.
    custom_objects.objects[("sandboxclaims", "foreign")] = {
        "metadata": {"name": "foreign", "creationTimestamp": "2026-09-01T12:00:00Z"},
        "spec": {"warmPoolRef": {"name": "other"}},
    }


async def test_list_derives_each_provisioning_state_from_the_cr_graph(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    views = {view.name: view for view in await inventory.list_sandboxes(include_archived=True)}

    assert {name: view.state for name, view in views.items()} == {
        "fresh": ProvisioningState.CLAIM_CREATED,
        "unassigned": ProvisioningState.WAITING_FOR_SANDBOX,
        "podless": ProvisioningState.WAITING_FOR_POD,
        "starting": ProvisioningState.WAITING_FOR_POD_READY,
        "live": ProvisioningState.RUNNING,
        "paused": ProvisioningState.SUSPENDED,
        "shelved": ProvisioningState.ARCHIVED,
    }
    live = views["live"]
    assert (live.provider, live.model) == (Provider.CODEX, "cheap-codex")
    assert (live.sandbox_name, live.pod_name, live.pod_phase, live.pod_ip) == (
        "sb-live",
        "sb-live",
        "Running",
        "10.0.0.7",
    )
    assert views["podless"].pod_name is None
    assert views["shelved"].archived
    assert not views["live"].archived


async def test_list_hides_archived_sandboxes_unless_asked(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    names = {view.name for view in await inventory.list_sandboxes()}

    assert "shelved" not in names
    assert "live" in names


async def test_get_reads_one_sandbox_and_refuses_foreign_or_missing_claims(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    view = await inventory.get("live")

    assert (view.state, view.pod_ip) == (ProvisioningState.RUNNING, "10.0.0.7")
    with pytest.raises(SandboxNotFoundError):
        await inventory.get("foreign")
    with pytest.raises(SandboxNotFoundError):
        await inventory.get("never-made")


async def test_create_labels_the_claim_and_references_the_pool(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    view = await inventory.create(NewSandbox(slug="my-task", provider=Provider.CLAUDE, model="claude-cheap"))

    assert re.fullmatch(r"my-task-[a-z0-9]{5}", view.name)
    assert view.state == ProvisioningState.CLAIM_CREATED
    stored = custom_objects.objects[("sandboxclaims", view.name)]
    assert stored["metadata"]["labels"] == {MANAGED_LABEL: "true", PROVIDER_LABEL: "claude"}
    assert stored["metadata"]["annotations"] == {MODEL_ANNOTATION: "claude-cheap"}
    assert stored["spec"] == {
        "warmPoolRef": {"name": "agentplane-test-pool"},
        "lifecycle": {"shutdownPolicy": "Retain"},
    }


async def test_create_names_each_sandbox_uniquely(inventory: SandboxInventory) -> None:
    spec = NewSandbox(slug="twice", provider=Provider.CODEX, model="m")

    first, second = await inventory.create(spec), await inventory.create(spec)

    assert first.name != second.name


async def test_suspend_and_resume_patch_the_sandbox_operating_mode(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.suspend("live")
    suspended = await inventory.get("live")
    await inventory.resume("live")
    resumed = await inventory.get("live")

    assert custom_objects.patches == [
        ("sandboxes", "sb-live", {"spec": {"operatingMode": "Suspended"}}),
        ("sandboxes", "sb-live", {"spec": {"operatingMode": "Running"}}),
    ]
    assert (suspended.state, resumed.state) == (ProvisioningState.SUSPENDED, ProvisioningState.RUNNING)


async def test_suspend_needs_a_sandbox_to_suspend(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    with pytest.raises(SandboxNotProvisionedError):
        await inventory.suspend("unassigned")
    with pytest.raises(SandboxNotProvisionedError):
        await inventory.resume("fresh")
    assert custom_objects.patches == []


async def test_archive_suspends_then_labels(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.archive("live")

    assert custom_objects.patches == [
        ("sandboxes", "sb-live", {"spec": {"operatingMode": "Suspended"}}),
        ("sandboxclaims", "live", {"metadata": {"labels": {ARCHIVED_LABEL: "true"}}}),
    ]
    assert (await inventory.get("live")).state == ProvisioningState.ARCHIVED
    assert "live" not in {view.name for view in await inventory.list_sandboxes()}


async def test_unarchive_clears_the_label_and_leaves_the_sandbox_suspended(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.unarchive("shelved")

    assert custom_objects.patches == [("sandboxclaims", "shelved", {"metadata": {"labels": {ARCHIVED_LABEL: None}}})]
    assert ARCHIVED_LABEL not in custom_objects.objects[("sandboxclaims", "shelved")]["metadata"]["labels"]
    assert (await inventory.get("shelved")).state == ProvisioningState.SUSPENDED


async def test_delete_removes_the_claim_only(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    """The controller, not the app, takes the Sandbox and PVC down behind the claim."""
    _populate_one_of_each_state(custom_objects, core_v1)

    await inventory.delete("live")

    assert custom_objects.deleted == [("sandboxclaims", "live")]
    assert ("sandboxes", "sb-live") in custom_objects.objects
    with pytest.raises(SandboxNotFoundError):
        await inventory.delete("live")


if __name__ == "__main__":
    pytest_bazel.main()
