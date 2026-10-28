"""The inventory's reading of the Sandbox and Pod, and the objects each operation writes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client

from agentplane.proto.sandboxes_pb2 import CreateSandboxRequest, SandboxPhase
from agentplane.sandbox_resource import PRESET_LABEL, SANDBOXES_PLURAL, OperatingMode
from agentplane.sandboxes.errors import (
    SandboxConflictError,
    SandboxModeUnknownError,
    SandboxNotFoundError,
    SandboxQuiescingError,
    SandboxRunningError,
    SandboxTearingDownError,
    UnknownPolicySetError,
)
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.kubernetes_views import CREATE_INTENT
from agentplane.sandboxes.testing.sandbox_namespace import TEMPLATE, SandboxNamespace
from agentplane.subjects import ServiceAccountRef

CALLER = ServiceAccountRef(namespace="test-callers", name="test-caller")
OTHER = ServiceAccountRef(namespace="test-callers", name="other-caller")

_READY = {
    "type": "Ready",
    "status": "True",
    "reason": "PodReady",
    "message": "",
    "lastTransitionTime": "2026-09-01T12:00:00Z",
}
_QUIESCING = {
    "type": "Running",
    "status": "False",
    "reason": "Quiescing",
    "message": "waiting for the runner to seal",
    "lastTransitionTime": "2026-09-01T12:00:00Z",
}
# Stands in for the Sandbox Controller's finalizer, which holds a deleted Sandbox until its Sessions are sealed.
_SEALING = "test.invalid/seal-sessions"


async def _populate_one_of_each_state(namespace: SandboxNamespace) -> None:
    # `podless` reports an incarnation whose Pod is gone.
    await namespace.sandbox("podless")
    await namespace.pod("starting", phase="Pending", ready=False, ip=None, waiting_reason="ImagePullBackOff")
    await namespace.sandbox("starting")
    await namespace.pod("live", phase="Running", ready=True, ip="10.0.0.7")
    await namespace.sandbox("live", conditions=[_READY])
    await namespace.sandbox("paused", running=False)


async def _stored(namespace: SandboxNamespace, name: str) -> dict[str, Any]:
    return await namespace.read(SANDBOXES_PLURAL, name)


async def test_list_keeps_each_resource_status_separate(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)

    views = {view.name: view for view in await inventory.list_sandboxes()}

    assert {name: (view.operating_mode, view.HasField("pod")) for name, view in views.items()} == {
        "podless": (OperatingMode.RUNNING, False),
        "starting": (OperatingMode.RUNNING, True),
        "live": (OperatingMode.RUNNING, True),
        "paused": (OperatingMode.SUSPENDED, False),
    }
    live = views["live"]
    assert live.HasField("pod")
    sandbox_status = MessageToDict(live.status)
    assert sandbox_status["conditions"] == [_READY]
    assert sandbox_status["incarnation"] == {"name": "live", "uid": live.pod.uid}
    assert sandbox_status["operatingMode"] == "Running"
    assert "containerStatuses" not in sandbox_status
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
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)

    view = await inventory.get("live")

    assert view.operating_mode == OperatingMode.RUNNING
    assert MessageToDict(view.pod.status)["podIP"] == "10.0.0.7"
    with pytest.raises(SandboxNotFoundError):
        await inventory.get("never-made")


async def test_create_writes_a_running_sandbox_of_the_template(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    spec = CreateSandboxRequest(name="my-task", template=TEMPLATE, bootstrap="printf boot")
    view = await inventory.create(spec, policy_sets={"test-b", "test-a"}, caller=CALLER)

    assert view.name == "my-task"
    assert not view.HasField("operating_mode")
    assert (view.service_account.name, view.binding.bootstrap) == ("my-task", "printf boot")
    stored = await _stored(namespace, "my-task")
    assert CREATE_INTENT in stored["metadata"]["annotations"]
    # `wakeGeneration` is the CRD's default.
    assert stored["spec"] == {
        "template": TEMPLATE,
        "running": True,
        "policySets": ["test-a", "test-b"],
        "bootstrap": "printf boot",
        "wakeOnSend": False,
        "wakeGeneration": 0,
    }


async def test_wake_policy_is_uid_fenced_and_does_not_resume_or_wake(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    spec = CreateSandboxRequest(name="wake-policy", template=TEMPLATE, wake_on_send=True)
    view = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    assert (await _stored(namespace, view.name))["spec"]["wakeOnSend"] is True

    await inventory.set_wake_on_send(view.name, uid=view.uid, wake_on_send=False)

    stored = await _stored(namespace, view.name)
    assert stored["spec"]["wakeOnSend"] is False
    assert stored["spec"]["running"] is True
    assert stored["spec"]["wakeGeneration"] == 0

    # The same name, another incarnation.
    await namespace.delete(SANDBOXES_PLURAL, view.name)
    await inventory.create(spec, policy_sets=set(), caller=CALLER)
    with pytest.raises(SandboxNotFoundError):
        await inventory.set_wake_on_send(view.name, uid=view.uid, wake_on_send=False)
    assert (await _stored(namespace, view.name))["spec"]["wakeOnSend"] is True


async def test_wake_policy_refuses_a_sandbox_that_is_being_deleted(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    view = await inventory.create(
        CreateSandboxRequest(name="deleting-wake-policy", template=TEMPLATE), policy_sets=set(), caller=CALLER
    )
    await namespace.patch(SANDBOXES_PLURAL, view.name, {"metadata": {"finalizers": [_SEALING]}})
    await namespace.delete(SANDBOXES_PLURAL, view.name)

    with pytest.raises(SandboxTearingDownError):
        await inventory.set_wake_on_send(view.name, uid=view.uid, wake_on_send=True)

    assert (await _stored(namespace, view.name))["spec"]["wakeOnSend"] is False


async def test_create_records_the_preset_as_a_label_and_reads_it_back(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    spec = CreateSandboxRequest(name="from-preset", template=TEMPLATE, preset="test-preset")
    view = await inventory.create(spec, policy_sets=set(), caller=CALLER)

    assert (await _stored(namespace, "from-preset"))["metadata"]["labels"] == {PRESET_LABEL: "test-preset"}
    assert view.preset == "test-preset"
    bare = await inventory.create(
        CreateSandboxRequest(name="bare", template=TEMPLATE), policy_sets=set(), caller=CALLER
    )
    assert not bare.HasField("preset")


async def test_policy_sets_must_exist(inventory: SandboxInventory, namespace: SandboxNamespace) -> None:
    await namespace.egress_policy_set("test-known", [{"hosts": ["known.test"]}])
    await inventory.require_policy_sets({"test-known"})
    with pytest.raises(UnknownPolicySetError):
        await inventory.require_policy_sets({"test-known", "test-unknown"})


async def test_create_retries_the_same_name(inventory: SandboxInventory) -> None:
    spec = CreateSandboxRequest(name="twice", template=TEMPLATE)

    first = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    retry = await inventory.retry(spec, caller=CALLER)

    assert retry is not None
    assert (retry.name, retry.uid) == ("twice", first.uid)


async def test_suspend_and_resume_patch_running(inventory: SandboxInventory, namespace: SandboxNamespace) -> None:
    await _populate_one_of_each_state(namespace)

    live = await inventory.get("live")
    await inventory.suspend("live", uid=live.uid)
    assert (await _stored(namespace, "live"))["spec"]["running"] is False
    # The app changed desired state; only the controller changes effective mode.
    suspended = await inventory.get("live")
    assert suspended.operating_mode == OperatingMode.RUNNING
    await namespace.patch_status(SANDBOXES_PLURAL, "live", {"operatingMode": "Suspended"})
    suspended = await inventory.get("live")
    await inventory.resume("live", uid=live.uid)
    await inventory.resume("live", uid=live.uid)
    assert (await inventory.get("live")).operating_mode == OperatingMode.SUSPENDED
    # Asked twice, woken once.
    stored = (await _stored(namespace, "live"))["spec"]
    assert (stored["running"], stored["wakeGeneration"]) == (True, 1)
    await namespace.patch_status(SANDBOXES_PLURAL, "live", {"operatingMode": "Running"})
    resumed = await inventory.get("live")

    assert (suspended.operating_mode, resumed.operating_mode) == (OperatingMode.SUSPENDED, OperatingMode.RUNNING)
    with pytest.raises(SandboxNotFoundError):
        await inventory.suspend("live", uid="test-other-incarnation")


async def test_published_operating_mode_overrides_spec_running(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)
    # Idle-suspended: the spec still says run, and the controller says it does not.
    await namespace.patch(SANDBOXES_PLURAL, "live", {"spec": {"wakeGeneration": 4}})
    await namespace.patch_status(
        SANDBOXES_PLURAL, "live", {"operatingMode": "Suspended", "suspendedBy": "Idle", "suspendedAtWakeGeneration": 4}
    )

    live = await inventory.get("live")
    assert live.operating_mode == OperatingMode.SUSPENDED
    await inventory.begin_teardown("live", uid=live.uid)
    assert "live" not in await namespace.names(SANDBOXES_PLURAL)


async def test_unknown_mode_cannot_be_deleted_or_woken(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)
    await namespace.patch(SANDBOXES_PLURAL, "live", {"spec": {"wakeOnSend": True}})
    fresh = await namespace.patch_status(SANDBOXES_PLURAL, "live", {"operatingMode": None})
    view = await inventory.get("live")
    before_resume_calls: list[int] = []

    async def before_resume(generation: int) -> None:
        before_resume_calls.append(generation)

    assert not view.HasField("operating_mode")
    with pytest.raises(SandboxModeUnknownError):
        await inventory.begin_teardown("live", uid=view.uid)
    with pytest.raises(SandboxModeUnknownError):
        await inventory.wake_for_send("live", uid=view.uid, before_resume=before_resume)
    assert before_resume_calls == []
    # Neither deleted nor patched.
    assert (await _stored(namespace, "live"))["metadata"]["resourceVersion"] == fresh["metadata"]["resourceVersion"]


async def test_resume_wakes_an_idle_suspended_sandbox_by_bumping_the_generation(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)
    await namespace.patch(SANDBOXES_PLURAL, "live", {"spec": {"wakeGeneration": 4}})
    idle = await namespace.patch_status(
        SANDBOXES_PLURAL, "live", {"operatingMode": "Suspended", "suspendedBy": "Idle", "suspendedAtWakeGeneration": 4}
    )
    awake = await _stored(namespace, "starting")

    await inventory.resume("live", uid=idle["metadata"]["uid"])
    await inventory.resume("live", uid=idle["metadata"]["uid"])
    await inventory.resume("starting", uid=awake["metadata"]["uid"])

    assert {key: (await _stored(namespace, "live"))["spec"][key] for key in ("running", "wakeGeneration")} == {
        "running": True,
        "wakeGeneration": 5,
    }
    assert (await _stored(namespace, "starting"))["metadata"]["resourceVersion"] == awake["metadata"]["resourceVersion"]


@pytest.mark.parametrize("idle_suspended", [False, True], ids=["explicit-stop", "idle-stop"])
async def test_concurrent_resumes_increment_the_wake_generation_once(
    inventory: SandboxInventory, namespace: SandboxNamespace, monkeypatch: pytest.MonkeyPatch, idle_suspended: bool
) -> None:
    await _populate_one_of_each_state(namespace)
    await namespace.patch(SANDBOXES_PLURAL, "paused", {"spec": {"running": idle_suspended, "wakeGeneration": 4}})
    paused = await namespace.patch_status(
        SANDBOXES_PLURAL,
        "paused",
        {
            "operatingMode": "Suspended",
            **({"suspendedBy": "Idle", "suspendedAtWakeGeneration": 4} if idle_suspended else {}),
        },
    )
    read_version = paused["metadata"]["resourceVersion"]
    original_patch = namespace.custom_objects.patch_namespaced_custom_object
    both_read = asyncio.Barrier(2)
    attempted_versions: list[str] = []

    async def patch_after_both_reads(
        group: str, version: str, namespace_name: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        assert isinstance(body, dict)
        resource_version = body["metadata"]["resourceVersion"]
        attempted_versions.append(resource_version)
        if resource_version == read_version:
            await both_read.wait()
        return await original_patch(group, version, namespace_name, plural, name, body, _content_type=_content_type)

    monkeypatch.setattr(namespace.custom_objects, "patch_namespaced_custom_object", patch_after_both_reads)
    uid = paused["metadata"]["uid"]
    async with asyncio.timeout(10):
        await asyncio.gather(inventory.resume("paused", uid=uid), inventory.resume("paused", uid=uid))

    # Both wrote against what they read; the API server refused the second, whose retry found it woken.
    assert attempted_versions == [read_version, read_version]
    stored = await _stored(namespace, "paused")
    assert stored["metadata"]["uid"] == uid
    assert stored["spec"]["running"] is True
    assert stored["spec"]["wakeGeneration"] == 5


async def test_resume_refuses_to_race_the_controller_quiescing_an_incarnation(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)
    paused = await namespace.patch_status(SANDBOXES_PLURAL, "paused", {"conditions": [_QUIESCING]})

    with pytest.raises(SandboxQuiescingError, match="retry resume after it is suspended"):
        await inventory.resume("paused", uid=paused["metadata"]["uid"])

    stored = await _stored(namespace, "paused")
    assert stored["spec"]["running"] is False
    assert stored["metadata"]["resourceVersion"] == paused["metadata"]["resourceVersion"]


async def test_resume_rechecks_quiescing_after_a_resource_version_conflict(
    inventory: SandboxInventory, namespace: SandboxNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _populate_one_of_each_state(namespace)
    paused = await _stored(namespace, "paused")
    original_patch = namespace.custom_objects.patch_namespaced_custom_object
    attempts = 0

    async def quiesce_first(
        group: str, version: str, namespace_name: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # The controller starts quiescing between the inventory's read and its write.
            await namespace.patch_status(SANDBOXES_PLURAL, "paused", {"conditions": [_QUIESCING]})
        return await original_patch(group, version, namespace_name, plural, name, body, _content_type=_content_type)

    monkeypatch.setattr(namespace.custom_objects, "patch_namespaced_custom_object", quiesce_first)
    with pytest.raises(SandboxQuiescingError):
        await inventory.resume("paused", uid=paused["metadata"]["uid"])

    assert attempts == 1
    assert (await _stored(namespace, "paused"))["spec"]["running"] is False


async def test_teardown_deletes_a_suspended_sandbox_fenced_until_sealed(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    await _populate_one_of_each_state(namespace)
    await namespace.patch(SANDBOXES_PLURAL, "paused", {"metadata": {"finalizers": [_SEALING]}})
    live, paused = await inventory.get("live"), await inventory.get("paused")
    with pytest.raises(SandboxRunningError):
        await inventory.begin_teardown("live", uid=live.uid)
    await inventory.begin_teardown("paused", uid=paused.uid)
    deleting = await _stored(namespace, "paused")
    assert deleting["metadata"]["deletionTimestamp"]
    for _ in range(2):
        assert (await inventory.get("paused")).phase == SandboxPhase.SANDBOX_PHASE_QUIESCING
        # Asking again changes nothing.
        await inventory.begin_teardown("paused", uid=paused.uid)
        assert (await _stored(namespace, "paused"))["metadata"] == deleting["metadata"]
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
    inventory: SandboxInventory, namespace: SandboxNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = namespace.custom_objects.create_namespaced_custom_object

    async def committed_then_lost(
        group: str, version: str, namespace_name: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """The API server commits the create and its reply is lost on the way back: a transport fault
        the server itself cannot be made to produce."""
        await original(group, version, namespace_name, plural, body)
        raise k8s_client.ApiException(status=503)

    monkeypatch.setattr(namespace.custom_objects, "create_namespaced_custom_object", committed_then_lost)
    view = await inventory.create(
        CreateSandboxRequest(name="recover", template=TEMPLATE), policy_sets=set(), caller=CALLER
    )
    assert view.uid == (await _stored(namespace, "recover"))["metadata"]["uid"]


async def test_create_refused_by_the_api_server_raises(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    """`create` leaves a template's presence to the CRD, which refuses a Sandbox without one."""
    with pytest.raises(k8s_client.ApiException) as refused:
        await inventory.create(CreateSandboxRequest(name="refused", template=""), policy_sets=set(), caller=CALLER)
    assert refused.value.status == 422
    assert await namespace.names(SANDBOXES_PLURAL) == set()


async def test_concurrent_creates_share_one_sandbox(
    inventory: SandboxInventory, namespace: SandboxNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = namespace.custom_objects.create_namespaced_custom_object
    release = asyncio.Event()
    arrived = 0

    async def collide(
        group: str, version: str, namespace_name: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            release.set()
        await release.wait()
        return await original(group, version, namespace_name, plural, body)

    monkeypatch.setattr(namespace.custom_objects, "create_namespaced_custom_object", collide)
    spec = CreateSandboxRequest(name="concurrent", template=TEMPLATE)
    first, second = await asyncio.gather(
        inventory.create(spec, policy_sets=set(), caller=CALLER),
        inventory.create(spec, policy_sets=set(), caller=CALLER),
    )
    assert first.uid == second.uid
    assert await namespace.names(SANDBOXES_PLURAL) == {"concurrent"}


async def test_deleted_name_is_reusable_but_not_the_old_uid(
    inventory: SandboxInventory, namespace: SandboxNamespace
) -> None:
    spec = CreateSandboxRequest(name="reused", template=TEMPLATE)
    previous = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    await inventory.suspend(previous.name, uid=previous.uid)
    # Only a controller report, not the app's desired-state patch, proves it is suspended.
    await namespace.patch_status(SANDBOXES_PLURAL, previous.name, {"operatingMode": "Suspended"})
    await inventory.begin_teardown(previous.name, uid=previous.uid)
    assert await inventory.retry(spec, caller=CALLER) is None
    following = await inventory.create(spec, policy_sets=set(), caller=CALLER)
    assert following.uid != previous.uid
    with pytest.raises(SandboxNotFoundError):
        await inventory.suspend(previous.name, uid=previous.uid)


if __name__ == "__main__":
    pytest_bazel.main()
