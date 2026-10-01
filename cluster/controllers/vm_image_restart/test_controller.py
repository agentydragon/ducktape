"""Controller reconciliation against small fake Kubernetes API clients."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
import pytest_bazel
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException

from cluster.controllers.vm_image_restart import controller
from cluster.controllers.vm_image_restart.settings import Settings

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_SETTINGS = Settings(
    target_namespace="vm-image-restart-test", target_vm_name="example-vm", state_configmap_name="vm-image-restart-state"
)
_OLD_IMAGE = "registry.invalid/appliance:old"
_NEW_IMAGE = "registry.invalid/appliance:new"


class FakeVMApi:
    def __init__(
        self, vm: dict[str, Any], vmi: dict[str, Any] | None, *, delete_errors: list[ApiException] | None = None
    ) -> None:
        self.vm = vm
        self.vmi = vmi
        self.delete_errors = list(delete_errors or [])
        self.delete_calls: list[dict[str, Any]] = []

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        if plural == "virtualmachines":
            return self.vm
        if self.vmi is None:
            raise ApiException(status=404, reason="not found")
        return self.vmi

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: client.V1DeleteOptions
    ) -> None:
        self.delete_calls.append(
            {"group": group, "version": version, "namespace": namespace, "plural": plural, "name": name, "body": body}
        )
        if self.delete_errors:
            raise self.delete_errors.pop(0)


class FakeCoreApi:
    def __init__(self, state: controller.RestartState | None = None) -> None:
        self.state = _encode_state(state) if state is not None else None
        self.events: list[client.CoreV1Event] = []

    async def read_namespaced_config_map(self, name: str, namespace: str) -> client.V1ConfigMap:
        data = {controller.STATE_KEY: self.state} if self.state is not None else None
        return client.V1ConfigMap(data=data)

    async def patch_namespaced_config_map(self, name: str, namespace: str, body: dict[str, Any]) -> None:
        self.state = body["data"][controller.STATE_KEY]

    async def create_namespaced_event(self, namespace: str, body: client.CoreV1Event) -> None:
        self.events.append(body)


def _vm(
    *, generation: int = 2, observed_generation: int = 1, image: str = _NEW_IMAGE, restart_required: bool = True
) -> dict[str, Any]:
    conditions = [{"type": "RestartRequired", "status": "True"}] if restart_required else []
    return {
        "metadata": {
            "name": _SETTINGS.target_vm_name,
            "namespace": _SETTINGS.target_namespace,
            "uid": "vm-uid",
            "generation": generation,
            "annotations": {controller.OPT_IN_ANNOTATION: "true"},
        },
        "status": {
            "desiredGeneration": generation,
            "observedGeneration": observed_generation,
            "conditions": conditions,
        },
        "spec": {"template": {"spec": {"volumes": [_rootdisk(image)]}}},
    }


def _vmi(*, uid: str, image: str, ready: bool = True, deleting: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {"uid": uid}
    if deleting:
        metadata["deletionTimestamp"] = _NOW.isoformat()
    conditions = [{"type": "Ready", "status": "True"}] if ready else []
    return {"metadata": metadata, "status": {"conditions": conditions}, "spec": {"volumes": [_rootdisk(image)]}}


def _rootdisk(image: str) -> dict[str, Any]:
    return {"name": "rootdisk", "containerDisk": {"image": image}}


def _state(
    *,
    generation: int = 2,
    image: str = _NEW_IMAGE,
    old_uid: str | None = "old-vmi",
    attempts: int = 0,
    phase: Literal["Restarting", "Ready", "Failed"] = "Restarting",
    started_at: datetime = _NOW,
    next_attempt_at: datetime = _NOW,
    finished_at: datetime | None = None,
    message: str = "waiting for the new VMI",
) -> controller.RestartState:
    return controller.RestartState(
        generation=generation,
        image=image,
        old_vmi_uid=old_uid,
        attempts=attempts,
        phase=phase,
        started_at=started_at,
        next_attempt_at=next_attempt_at,
        finished_at=finished_at,
        message=message,
    )


def _encode_state(state: controller.RestartState | None) -> str | None:
    if state is None:
        return None
    return state.model_dump_json(by_alias=True, exclude_none=True)


def _stored_state(core: FakeCoreApi) -> controller.RestartState:
    assert core.state is not None
    return controller.RestartState.model_validate_json(core.state)


def _event_reasons(core: FakeCoreApi) -> list[str | None]:
    return [event.reason for event in core.events]


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "_utcnow", lambda: _NOW)


async def test_initial_restart_persists_attempt_and_gracefully_deletes_vmi() -> None:
    vm_api = FakeVMApi(_vm(), _vmi(uid="old-vmi", image=_OLD_IMAGE))
    core = FakeCoreApi()

    await controller.reconcile(vm_api, core, _SETTINGS)

    assert len(vm_api.delete_calls) == 1
    assert vm_api.delete_calls[0]["namespace"] == _SETTINGS.target_namespace
    assert vm_api.delete_calls[0]["name"] == _SETTINGS.target_vm_name
    assert vm_api.delete_calls[0]["body"].grace_period_seconds == controller.RESTART_GRACE_SECONDS
    state = _stored_state(core)
    assert state.phase == "Restarting"
    assert state.attempts == 1
    assert state.old_vmi_uid == "old-vmi"
    assert _event_reasons(core) == ["VMImageRestartRequested"]


async def test_replacement_is_ready_only_after_new_vmi_is_ready_on_desired_image() -> None:
    vm_api = FakeVMApi(_vm(observed_generation=2, restart_required=False), _vmi(uid="new-vmi", image=_NEW_IMAGE))
    core = FakeCoreApi(_state())

    await controller.reconcile(vm_api, core, _SETTINGS)

    assert _stored_state(core).phase == "Ready"
    assert _event_reasons(core) == ["VMImageRestartReady"]
    assert vm_api.delete_calls == []


async def test_new_generation_resets_attempts_and_waits_for_terminating_vmi() -> None:
    vm_api = FakeVMApi(
        _vm(generation=3, observed_generation=2, image="registry.invalid/appliance:newer"),
        _vmi(uid="old-vmi", image=_OLD_IMAGE, ready=False, deleting=True),
    )
    core = FakeCoreApi(_state(attempts=2))

    await controller.reconcile(vm_api, core, _SETTINGS)

    state = _stored_state(core)
    assert state.generation == 3
    assert state.image == "registry.invalid/appliance:newer"
    assert state.attempts == 0
    assert state.old_vmi_uid == "old-vmi"
    assert vm_api.delete_calls == []


async def test_restart_api_failures_are_backed_off_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [_NOW]
    monkeypatch.setattr(controller, "_utcnow", lambda: clock[0])
    error = ApiException(status=503, reason="apiserver unavailable")
    vm_api = FakeVMApi(_vm(), _vmi(uid="old-vmi", image=_OLD_IMAGE), delete_errors=[error, error, error])
    core = FakeCoreApi()

    await controller.reconcile(vm_api, core, _SETTINGS)
    assert len(vm_api.delete_calls) == 1
    assert _stored_state(core).attempts == 1

    # The persisted backoff prevents another delete at the same time.
    await controller.reconcile(vm_api, core, _SETTINGS)
    assert len(vm_api.delete_calls) == 1

    while _stored_state(core).phase != "Failed":
        clock[0] = _stored_state(core).next_attempt_at + timedelta(seconds=1)
        await controller.reconcile(vm_api, core, _SETTINGS)

    state = _stored_state(core)
    assert len(vm_api.delete_calls) == controller.MAX_RESTART_ATTEMPTS
    assert state.attempts == controller.MAX_RESTART_ATTEMPTS
    assert state.phase == "Failed"
    assert _event_reasons(core)[-1] == "VMImageRestartFailed"


async def test_rollout_timeout_marks_state_failed_without_an_extra_delete() -> None:
    old = _state(started_at=_NOW - timedelta(minutes=31))
    vm_api = FakeVMApi(_vm(), _vmi(uid="old-vmi", image=_OLD_IMAGE))
    core = FakeCoreApi(old)

    await controller.reconcile(vm_api, core, _SETTINGS)

    assert _stored_state(core).phase == "Failed"
    assert _event_reasons(core) == ["VMImageRestartTimedOut"]
    assert vm_api.delete_calls == []


async def test_failed_rollout_records_later_kubevirt_recovery() -> None:
    failed = _state(
        attempts=controller.MAX_RESTART_ATTEMPTS,
        phase="Failed",
        started_at=_NOW - timedelta(minutes=31),
        finished_at=_NOW - timedelta(minutes=1),
        message="gave up waiting",
    )
    vm_api = FakeVMApi(_vm(observed_generation=2, restart_required=False), _vmi(uid="recovered-vmi", image=_NEW_IMAGE))
    core = FakeCoreApi(failed)

    await controller.reconcile(vm_api, core, _SETTINGS)

    assert _stored_state(core).phase == "Ready"
    assert _event_reasons(core) == ["VMImageRestartRecovered"]
    assert vm_api.delete_calls == []


async def test_invalid_persisted_state_is_ignored() -> None:
    core = FakeCoreApi()
    core.state = '{"generation":"not-an-integer"}'

    assert await controller._read_state(core, _SETTINGS) is None


if __name__ == "__main__":
    pytest_bazel.main()
