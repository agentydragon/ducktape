"""The inventory's reading of the Sandbox and Pod, and the objects each operation writes."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import ANY

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client

from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.proto.runner_pb2 import HARNESS_CODEX
from agentplane.proto.sandboxes_pb2 import CreateSandboxRequest, SandboxPhase, SessionDefaults
from agentplane.sandbox_resource import PRESET_LABEL, SANDBOXES_PLURAL, OperatingMode
from agentplane.sandboxes.errors import (
    SandboxConflictError,
    SandboxNotFoundError,
    SandboxRunningError,
    SandboxTearingDownError,
    UnknownPolicySetError,
)
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.kubernetes_views import CREATE_INTENT
from agentplane.sandboxes.testing.fake_inventory import TEMPLATE, FakeCoreV1Api, FakeCustomObjectsApi, pod, sandbox
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf

CALLER = ServiceAccountRef(namespace="agentplane-test", name="test-caller")
OTHER = ServiceAccountRef(namespace="agentplane-test", name="other-caller")

_READY = {
    "type": "Ready",
    "status": "True",
    "reason": "PodReady",
    "message": "",
    "lastTransitionTime": "2026-09-01T12:00:00Z",
}


def _populate_one_of_each_state(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> None:
    custom_objects.objects[(SANDBOXES_PLURAL, "podless")] = sandbox("podless")
    custom_objects.objects[(SANDBOXES_PLURAL, "starting")] = sandbox("starting")
    core_v1.pods["starting"] = pod("starting", phase="Pending", ready=False, ip=None, waiting_reason="ImagePullBackOff")
    custom_objects.objects[(SANDBOXES_PLURAL, "live")] = sandbox("live", conditions=[_READY])
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[(SANDBOXES_PLURAL, "paused")] = sandbox("paused", running=False)


async def test_list_keeps_each_resource_status_separate(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    views = {view.name: view for view in await inventory.list_sandboxes()}

    assert {name: (view.operating_mode, view.HasField("pod")) for name, view in views.items()} == {
        "podless": (OperatingMode.RUNNING, False),
        "starting": (OperatingMode.RUNNING, True),
        "live": (OperatingMode.RUNNING, True),
        "paused": (OperatingMode.SUSPENDED, False),
    }
    live = views["live"]
    assert live.HasField("pod")
    assert MessageToDict(live.status) == {
        "conditions": [_READY],
        "incarnation": {"name": "live", "uid": "test-pod-live"},
    }
    pod_status = MessageToDict(live.pod.status)
    assert (pod_status["phase"], pod_status["podIP"]) == ("Running", "10.0.0.7")
    assert pod_status["conditions"] == [{"type": "Ready", "status": "True"}]
    assert pod_status["containerStatuses"][0]["state"] == {"running": {}}
    # A Pod held up by its image is visible as such, so the app can say why nothing is running.
    starting_status = MessageToDict(views["starting"].pod.status)
    assert starting_status["containerStatuses"][0]["state"] == {
        "waiting": {"reason": "ImagePullBackOff", "message": "ImagePullBackOff on starting"}
    }
    assert not views["podless"].HasField("pod")


async def test_get_reads_one_sandbox_and_refuses_missing_ones(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    view = await inventory.get("live")

    assert view.operating_mode == OperatingMode.RUNNING
    assert MessageToDict(view.pod.status)["podIP"] == "10.0.0.7"
    with pytest.raises(SandboxNotFoundError):
        await inventory.get("never-made")


async def test_create_writes_a_running_sandbox_of_the_template(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    spec = CreateSandboxRequest(
        name="my-task",
        template=TEMPLATE,
        session_defaults=SessionDefaults(harness=HARNESS_CODEX, model="test-model"),
        bootstrap="printf boot",
    )
    view = await inventory.create(spec, policy_sets={"test-b", "test-a"}, caller=CALLER)

    assert (view.name, view.operating_mode) == ("my-task", OperatingMode.RUNNING)
    assert (view.service_account.name, view.binding.bootstrap) == ("my-task", "printf boot")
    stored = custom_objects.objects[(SANDBOXES_PLURAL, "my-task")]
    assert CREATE_INTENT in stored["metadata"]["annotations"]
    assert stored["spec"] == {
        "template": TEMPLATE,
        "running": True,
        "policySets": ["test-a", "test-b"],
        "sessionDefaults": {"harness": "HARNESS_CODEX", "model": "test-model"},
        "bootstrap": "printf boot",
    }


async def test_create_records_the_preset_as_a_label_and_reads_it_back(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    spec = CreateSandboxRequest(name="from-preset", template=TEMPLATE, preset="test-preset")
    view = await inventory.create(spec, policy_sets=set(), caller=CALLER)

    assert custom_objects.objects[(SANDBOXES_PLURAL, "from-preset")]["metadata"]["labels"] == {
        PRESET_LABEL: "test-preset"
    }
    assert view.preset == "test-preset"
    bare = await inventory.create(
        CreateSandboxRequest(name="bare", template=TEMPLATE), policy_sets=set(), caller=CALLER
    )
    assert not bare.HasField("preset")


async def test_policy_sets_must_exist(inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi) -> None:
    custom_objects.objects[(POLICY_SETS_PLURAL, "test-known")] = {"metadata": {"name": "test-known"}}
    await inventory.require_policy_sets({"test-known"})
    with pytest.raises(UnknownPolicySetError):
        await inventory.require_policy_sets({"test-known", "test-unknown"})


async def test_create_retries_the_same_name(inventory: SandboxInventory) -> None:
    spec = CreateSandboxRequest(name="twice", template=TEMPLATE)

    first = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    retry = await inventory.retry(spec, caller=CALLER)

    assert retry is not None
    assert (retry.name, retry.uid) == ("twice", first.uid)


async def test_suspend_and_resume_patch_running(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)

    live = await inventory.get("live")
    await inventory.suspend("live", uid=live.uid)
    suspended = await inventory.get("live")
    await inventory.resume("live", uid=live.uid)
    resumed = await inventory.get("live")

    assert custom_objects.patches == [
        (SANDBOXES_PLURAL, "live", {"metadata": {"uid": live.uid, "resourceVersion": ANY}, "spec": {"running": False}}),
        (
            SANDBOXES_PLURAL,
            "live",
            {"metadata": {"uid": live.uid, "resourceVersion": ANY}, "spec": {"running": True, "wakeGeneration": 1}},
        ),
    ]
    assert (suspended.operating_mode, resumed.operating_mode) == (OperatingMode.SUSPENDED, OperatingMode.RUNNING)
    with pytest.raises(SandboxNotFoundError):
        await inventory.suspend("live", uid="test-other-incarnation")


async def test_published_operating_mode_overrides_spec_running(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)
    # Idle-suspended: the spec still says run, and the controller says it does not.
    idle = custom_objects.objects[(SANDBOXES_PLURAL, "live")]
    idle["status"]["operatingMode"] = "Suspended"
    idle["spec"]["wakeGeneration"] = 4
    custom_objects.objects[(SANDBOXES_PLURAL, "starting")]["status"]["operatingMode"] = "Running"

    live = await inventory.get("live")
    assert live.operating_mode == OperatingMode.SUSPENDED
    await inventory.begin_teardown("live", uid=live.uid)
    assert custom_objects.deleted == [(SANDBOXES_PLURAL, "live")]


async def test_resume_wakes_an_idle_suspended_sandbox_by_bumping_the_generation(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)
    idle = custom_objects.objects[(SANDBOXES_PLURAL, "live")]
    idle["status"]["operatingMode"] = "Suspended"
    idle["spec"]["wakeGeneration"] = 4
    awake = custom_objects.objects[(SANDBOXES_PLURAL, "starting")]
    awake["status"]["operatingMode"] = "Running"

    await inventory.resume("live", uid=idle["metadata"]["uid"])
    await inventory.resume("starting", uid=awake["metadata"]["uid"])

    assert [(name, body["spec"]) for _, name, body in custom_objects.patches] == [
        ("live", {"running": True, "wakeGeneration": 5}),
        ("starting", {"running": True}),
    ]


async def test_teardown_deletes_a_suspended_sandbox_fenced_until_sealed(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    _populate_one_of_each_state(custom_objects, core_v1)
    # The Sandbox Controller's finalizer holds it until its Sessions are sealed.
    custom_objects.objects[(SANDBOXES_PLURAL, "paused")]["metadata"]["finalizers"] = ["test-finalizer"]
    live, paused = await inventory.get("live"), await inventory.get("paused")
    with pytest.raises(SandboxRunningError):
        await inventory.begin_teardown("live", uid=live.uid)
    for _ in range(2):
        await inventory.begin_teardown("paused", uid=paused.uid)
        assert (await inventory.get("paused")).phase == SandboxPhase.SANDBOX_PHASE_QUIESCING
    assert custom_objects.deleted == [(SANDBOXES_PLURAL, "paused")]
    for operation in (inventory.suspend, inventory.resume):
        with pytest.raises(SandboxTearingDownError):
            await operation("paused", uid=paused.uid)


async def test_create_intent_conflicts_and_caller_cannot_adopt_name(inventory: SandboxInventory) -> None:
    spec = CreateSandboxRequest(name="same", template=TEMPLATE)
    await inventory.create(spec, policy_sets=set(), caller=CALLER)
    with pytest.raises(SandboxConflictError):
        await inventory.retry(spec, caller=OTHER)
    with pytest.raises(SandboxConflictError):
        await inventory.retry(CreateSandboxRequest(name="same", template="other"), caller=CALLER)


async def test_create_recovers_a_lost_reply(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = custom_objects.create_namespaced_custom_object

    async def committed_then_lost(
        group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        await original(group, version, namespace, plural, body)
        raise k8s_client.ApiException(status=503)

    monkeypatch.setattr(custom_objects, "create_namespaced_custom_object", committed_then_lost)
    view = await inventory.create(
        CreateSandboxRequest(name="recover", template=TEMPLATE), policy_sets=set(), caller=CALLER
    )
    assert view.uid == custom_objects.objects[(SANDBOXES_PLURAL, "recover")]["metadata"]["uid"]


async def test_create_refused_by_the_api_server_raises(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi
) -> None:
    custom_objects.create_fails = True
    with pytest.raises(k8s_client.ApiException):
        await inventory.create(
            CreateSandboxRequest(name="refused", template=TEMPLATE), policy_sets=set(), caller=CALLER
        )


async def test_concurrent_creates_share_one_sandbox(
    inventory: SandboxInventory, custom_objects: FakeCustomObjectsApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = custom_objects.create_namespaced_custom_object
    release = asyncio.Event()
    arrived = 0

    async def collide(group: str, version: str, namespace: str, plural: str, body: dict[str, Any]) -> dict[str, Any]:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            release.set()
        await release.wait()
        return await original(group, version, namespace, plural, body)

    monkeypatch.setattr(custom_objects, "create_namespaced_custom_object", collide)
    spec = CreateSandboxRequest(name="concurrent", template=TEMPLATE)
    first, second = await asyncio.gather(
        inventory.create(spec, policy_sets=set(), caller=CALLER),
        inventory.create(spec, policy_sets=set(), caller=CALLER),
    )
    assert first.uid == second.uid
    assert len([key for key in custom_objects.objects if key[0] == SANDBOXES_PLURAL]) == 1


async def test_deleted_name_is_reusable_but_not_the_old_uid(inventory: SandboxInventory) -> None:
    spec = CreateSandboxRequest(name="reused", template=TEMPLATE)
    previous = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    await inventory.suspend(previous.name, uid=previous.uid)
    await inventory.begin_teardown(previous.name, uid=previous.uid)
    assert await inventory.retry(spec, caller=CALLER) is None
    following = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    assert following.uid != previous.uid
    with pytest.raises(SandboxNotFoundError):
        await inventory.suspend(previous.name, uid=previous.uid)


if __name__ == "__main__":
    pytest_bazel.main()
