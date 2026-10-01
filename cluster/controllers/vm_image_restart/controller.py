"""Restart a configured KubeVirt VMI after Flux stages a VM template update.

The configured VM is stateless and uses `runStrategy: Always`. KubeVirt marks
non-live-updatable template changes with `RestartRequired`, but leaves the old VMI
running. This controller watches one configured, explicitly opted-in VM through
namespaced list/watch streams. It stores rollout progress in a dedicated ConfigMap so a
pod restart cannot repeat a restart indefinitely.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cluster.controllers.vm_image_restart.settings import Settings
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to

CONTROLLER = "vm-image-restart"
OPT_IN_ANNOTATION = "ducktape.org/auto-restart-template-changes"
STATE_KEY = "state"
WATCH_TIMEOUT_SECONDS = 300
RECONCILE_RETRY_SECONDS = 30
MAX_RESTART_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (15, 60, 180)
ROLLOUT_TIMEOUT = timedelta(minutes=30)
RESTART_GRACE_SECONDS = 60

logger = logging.getLogger(CONTROLLER)


class RestartState(BaseModel):
    """Validated, persisted state for the one in-progress template rollout."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    generation: int = Field(ge=0)
    image: str = Field(min_length=1)
    old_vmi_uid: str | None = Field(default=None, alias="oldVmiUid")
    attempts: int = Field(default=0, ge=0, le=MAX_RESTART_ATTEMPTS)
    phase: Literal["Restarting", "Ready", "Failed"]
    started_at: datetime = Field(alias="startedAt")
    next_attempt_at: datetime = Field(alias="nextAttemptAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")
    message: str


class VMApi(Protocol):
    """KubeVirt API operations used by reconciliation."""

    def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> Awaitable[Any]: ...

    def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, *, body: client.V1DeleteOptions
    ) -> Awaitable[Any]: ...


class CoreApi(Protocol):
    """Core Kubernetes API operations used by reconciliation."""

    def read_namespaced_config_map(self, name: str, namespace: str) -> Awaitable[client.V1ConfigMap]: ...

    def patch_namespaced_config_map(self, name: str, namespace: str, body: dict[str, Any]) -> Awaitable[Any]: ...

    def create_namespaced_event(self, namespace: str, body: client.CoreV1Event) -> Awaitable[Any]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _container_disk_image(spec: dict[str, Any], volume_name: str = "rootdisk") -> str | None:
    for volume in spec.get("volumes", []):
        if volume.get("name") == volume_name:
            return (volume.get("containerDisk") or {}).get("image")
    return None


def _restart_required(vm: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "RestartRequired" and condition.get("status") == "True"
        for condition in vm.get("status", {}).get("conditions", [])
    )


def _ready(vmi: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in vmi.get("status", {}).get("conditions", [])
    )


async def _get_vmi(
    api: VMApi, settings: Settings, cache: Mapping[str, dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    if cache is not None:
        return cache.get(settings.target_vm_name)
    try:
        return cast(
            dict[str, Any],
            await api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=settings.target_namespace,
                plural="virtualmachineinstances",
                name=settings.target_vm_name,
            ),
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


async def _get_vm(
    api: VMApi, settings: Settings, cache: Mapping[str, dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    if cache is not None:
        return cache.get(settings.target_vm_name)
    try:
        return cast(
            dict[str, Any],
            await api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=settings.target_namespace,
                plural="virtualmachines",
                name=settings.target_vm_name,
            ),
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


async def _next_reconcile_delay(
    vm_api: VMApi,
    core: CoreApi,
    settings: Settings,
    *,
    vm_cache: Mapping[str, dict[str, Any]] | None = None,
    vmi_cache: Mapping[str, dict[str, Any]] | None = None,
) -> float | None:
    """Return the next retry/timeout timer; normal reconciles are watch-triggered."""
    state = await _read_state(core, settings)
    if state is None or state.phase != "Restarting":
        return None

    deadlines = [state.started_at + ROLLOUT_TIMEOUT]
    vm = await _get_vm(vm_api, settings, cache=vm_cache)
    if vm is None:
        return max(0.0, (deadlines[0] - _utcnow()).total_seconds())
    vmi = await _get_vmi(vm_api, settings, cache=vmi_cache)
    if vmi is not None:
        metadata = vmi.get("metadata", {})
        generation = int(vm.get("status", {}).get("desiredGeneration") or vm["metadata"].get("generation", 0))
        observed_generation = int(vm.get("status", {}).get("observedGeneration", 0))
        vmi_image = _container_disk_image(vmi.get("spec", {}))
        restart_still_needed = (
            _restart_required(vm)
            and (observed_generation < generation or vmi_image != state.image)
            and (vmi_image != state.image or _ready(vmi))
        )
        if (
            metadata.get("uid") == state.old_vmi_uid
            and not metadata.get("deletionTimestamp")
            and restart_still_needed
            and state.attempts < MAX_RESTART_ATTEMPTS
        ):
            deadlines.append(state.next_attempt_at)

    return max(0.0, (min(deadlines) - _utcnow()).total_seconds())


async def _read_state(core: CoreApi, settings: Settings) -> RestartState | None:
    try:
        config_map = await core.read_namespaced_config_map(
            name=settings.state_configmap_name, namespace=settings.target_namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            raise RuntimeError(
                f"required state ConfigMap {settings.target_namespace}/{settings.state_configmap_name} is missing"
            ) from exc
        raise
    raw = (config_map.data or {}).get(STATE_KEY)
    if not raw:
        return None
    try:
        return RestartState.model_validate_json(raw)
    except json.JSONDecodeError, ValidationError:
        logger.exception(
            "ignoring malformed restart state in %s/%s", settings.target_namespace, settings.state_configmap_name
        )
        return None


async def _write_state(core: CoreApi, settings: Settings, state: RestartState) -> None:
    # Merge-patch only our `data.state` key; Flux owns the ConfigMap object and its metadata.
    await core.patch_namespaced_config_map(
        name=settings.state_configmap_name,
        namespace=settings.target_namespace,
        body={
            "data": {
                STATE_KEY: json.dumps(
                    state.model_dump(mode="json", by_alias=True, exclude_none=True),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            }
        },
    )


async def _event(
    core: CoreApi, settings: Settings, vm: dict[str, Any], *, reason: str, message: str, event_type: str = "Normal"
) -> None:
    now = _utcnow()
    event = client.CoreV1Event(
        metadata=client.V1ObjectMeta(generate_name=f"{settings.target_vm_name}-", namespace=settings.target_namespace),
        involved_object=client.V1ObjectReference(
            api_version="kubevirt.io/v1",
            kind="VirtualMachine",
            name=settings.target_vm_name,
            namespace=settings.target_namespace,
            uid=vm.get("metadata", {}).get("uid"),
        ),
        reason=reason,
        message=message[:1024],
        source=client.V1EventSource(component=CONTROLLER),
        type=event_type,
        count=1,
        first_timestamp=now,
        last_timestamp=now,
    )
    try:
        await core.create_namespaced_event(namespace=settings.target_namespace, body=event)
    except ApiException:
        logger.exception(
            "could not publish %s event for %s/%s", reason, settings.target_namespace, settings.target_vm_name
        )


def _new_state(generation: int, image: str, vmi_uid: str | None) -> RestartState:
    now = _utcnow()
    return RestartState(
        generation=generation,
        image=image,
        old_vmi_uid=vmi_uid,
        attempts=0,
        phase="Restarting",
        started_at=now,
        next_attempt_at=now,
        message="waiting for the new VMI",
    )


async def _finish(
    *,
    core: CoreApi,
    settings: Settings,
    vm: dict[str, Any],
    state: RestartState,
    phase: Literal["Ready", "Failed"],
    message: str,
    reason: str,
) -> None:
    state.phase = phase
    state.message = message
    state.finished_at = _utcnow()
    await _write_state(core, settings, state)
    await _event(
        core, settings, vm, reason=reason, message=message, event_type="Warning" if phase == "Failed" else "Normal"
    )


async def _request_restart(
    *, vm_api: VMApi, core: CoreApi, settings: Settings, vm: dict[str, Any], state: RestartState, vmi: dict[str, Any]
) -> None:
    attempts = state.attempts
    if attempts >= MAX_RESTART_ATTEMPTS:
        await _finish(
            core=core,
            settings=settings,
            vm=vm,
            state=state,
            phase="Failed",
            reason="VMImageRestartFailed",
            message=f"gave up after {attempts} VMI restart requests; the desired image is {state.image}",
        )
        return

    now = _utcnow()
    if now < state.next_attempt_at:
        return
    attempts += 1
    backoff = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    state.attempts = attempts
    state.old_vmi_uid = vmi.get("metadata", {}).get("uid")
    state.next_attempt_at = now + timedelta(seconds=backoff)
    state.message = f"requested graceful VMI restart {attempts}/{MAX_RESTART_ATTEMPTS}"
    # Persist before deleting so a controller pod restart cannot issue an unbounded series
    # of restart requests while KubeVirt is still terminating the old VMI.
    await _write_state(core, settings, state)
    try:
        await vm_api.delete_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=settings.target_namespace,
            plural="virtualmachineinstances",
            name=settings.target_vm_name,
            body=client.V1DeleteOptions(grace_period_seconds=RESTART_GRACE_SECONDS),
        )
    except ApiException as exc:
        state.message = f"restart request {attempts}/{MAX_RESTART_ATTEMPTS} failed: {exc.reason or exc.status}"
        await _write_state(core, settings, state)
        logger.warning("VMI restart request failed: %s", exc)
        if attempts == MAX_RESTART_ATTEMPTS:
            await _finish(
                core=core,
                settings=settings,
                vm=vm,
                state=state,
                phase="Failed",
                reason="VMImageRestartFailed",
                message=state.message,
            )
        else:
            await _event(
                core, settings, vm, reason="VMImageRestartRetrying", message=state.message, event_type="Warning"
            )
        return

    message = f"requested graceful VMI restart for template generation {state.generation} ({state.image})"
    state.message = message
    await _write_state(core, settings, state)
    await _event(core, settings, vm, reason="VMImageRestartRequested", message=message)


async def reconcile(
    vm_api: VMApi,
    core: CoreApi,
    settings: Settings,
    *,
    vm_cache: Mapping[str, dict[str, Any]] | None = None,
    vmi_cache: Mapping[str, dict[str, Any]] | None = None,
) -> None:
    vm = await _get_vm(vm_api, settings, cache=vm_cache)
    if vm is None:
        return
    annotations = vm.get("metadata", {}).get("annotations", {})
    if annotations.get(OPT_IN_ANNOTATION) != "true":
        return

    generation = int(vm.get("status", {}).get("desiredGeneration") or vm["metadata"].get("generation", 0))
    observed_generation = int(vm.get("status", {}).get("observedGeneration", 0))
    desired_image = _container_disk_image(vm.get("spec", {}).get("template", {}).get("spec", {}))
    if not desired_image:
        logger.error("%s/%s has no rootdisk containerDisk image", settings.target_namespace, settings.target_vm_name)
        return

    vmi = await _get_vmi(vm_api, settings, cache=vmi_cache)
    state = await _read_state(core, settings)
    now = _utcnow()

    if state and state.generation != generation:
        if state.phase in {"Ready", "Failed"}:
            state = None
        else:
            # A newer Flux update superseded an in-progress rollout. Keep the original VMI UID
            # so the controller waits for the replacement already underway, but verify the
            # replacement against the latest template.
            state.generation = generation
            state.image = desired_image
            state.attempts = 0
            state.started_at = now
            state.next_attempt_at = now
            state.message = "newer VM template superseded the in-progress restart"
            await _write_state(core, settings, state)

    vmi_uid = (vmi or {}).get("metadata", {}).get("uid")
    vmi_image = _container_disk_image((vmi or {}).get("spec", {}))
    restart_required = _restart_required(vm)

    if state is None:
        if not restart_required or (observed_generation >= generation and vmi_image == desired_image):
            return
        state = _new_state(generation, desired_image, vmi_uid)
        await _write_state(core, settings, state)
        if vmi is None:
            return

    if state.phase == "Ready":
        return
    if state.phase == "Failed":
        # KubeVirt keeps retrying a `runStrategy: Always` VMI after transient image-pull or
        # scheduling failures. Record the eventual recovery without issuing another restart.
        if (
            vmi is not None
            and vmi_image == desired_image
            and _ready(vmi)
            and observed_generation >= generation
            and not restart_required
        ):
            await _finish(
                core=core,
                settings=settings,
                vm=vm,
                state=state,
                phase="Ready",
                reason="VMImageRestartRecovered",
                message=f"KubeVirt recovered the VMI on template generation {generation} ({desired_image})",
            )
        return

    if now - state.started_at >= ROLLOUT_TIMEOUT:
        await _finish(
            core=core,
            settings=settings,
            vm=vm,
            state=state,
            phase="Failed",
            reason="VMImageRestartTimedOut",
            message=(
                f"VMI did not become Ready on image {state.image} within "
                f"{int(ROLLOUT_TIMEOUT.total_seconds() // 60)} minutes"
            ),
        )
        return

    if vmi is None:
        return

    old_uid = state.old_vmi_uid
    if old_uid is None:
        state.old_vmi_uid = vmi_uid
        if vmi_image == state.image:
            # The VMI appeared after the controller began watching. Let a matching new image
            # finish booting before considering another restart for any remaining staged fields.
            state.next_attempt_at = now + timedelta(seconds=60)
        await _write_state(core, settings, state)
        old_uid = vmi_uid

    if vmi_uid != old_uid:
        if vmi_image == state.image:
            if _ready(vmi) and observed_generation >= generation and not restart_required:
                await _finish(
                    core=core,
                    settings=settings,
                    vm=vm,
                    state=state,
                    phase="Ready",
                    reason="VMImageRestartReady",
                    message=f"replacement VMI is Ready on template generation {generation} ({state.image})",
                )
            else:
                state.old_vmi_uid = vmi_uid
                state.next_attempt_at = now + timedelta(seconds=60)
                await _write_state(core, settings, state)
            return
        # KubeVirt recreated a VMI but it still reflects the old template. A further
        # graceful restart is allowed only within the same bounded attempt budget.
        state.old_vmi_uid = vmi_uid
        state.next_attempt_at = now
        await _write_state(core, settings, state)
        await _request_restart(vm_api=vm_api, core=core, settings=settings, vm=vm, state=state, vmi=vmi)
        return

    if _ready(vmi) and observed_generation >= generation and vmi_image == state.image and not restart_required:
        await _finish(
            core=core,
            settings=settings,
            vm=vm,
            state=state,
            phase="Ready",
            reason="VMImageRestartReady",
            message=f"VMI is Ready on template generation {generation} ({state.image})",
        )
        return

    if vmi_image == state.image and not _ready(vmi):
        # A VMI already using the new image can still be pulling or booting. `Always` handles
        # retrying failed launches; restarting it while it is coming up would create a loop.
        return

    if vmi.get("metadata", {}).get("deletionTimestamp"):
        return
    if restart_required and (observed_generation < generation or vmi_image != state.image):
        await _request_restart(vm_api=vm_api, core=core, settings=settings, vm=vm, state=state, vmi=vmi)


async def _run_reconcile_loop(
    vm_api: VMApi,
    core: CoreApi,
    settings: Settings,
    changed: asyncio.Event,
    *,
    vm_cache: Mapping[str, dict[str, Any]],
    vmi_cache: Mapping[str, dict[str, Any]],
) -> None:
    while True:
        await changed.wait()
        changed.clear()
        try:
            await reconcile(vm_api, core, settings, vm_cache=vm_cache, vmi_cache=vmi_cache)
            delay = await _next_reconcile_delay(vm_api, core, settings, vm_cache=vm_cache, vmi_cache=vmi_cache)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reconciliation failed for %s/%s", settings.target_namespace, settings.target_vm_name)
            delay = RECONCILE_RETRY_SECONDS

        if delay is not None:
            try:
                await asyncio.wait_for(changed.wait(), timeout=delay)
            except TimeoutError:
                changed.set()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    config.load_incluster_config()
    async with client.ApiClient() as api_client:
        vm_api = client.CustomObjectsApi(api_client)
        core = client.CoreV1Api(api_client)
        changed = asyncio.Event()
        changed.set()

        vm_cache: dict[str, dict[str, Any]] = {}
        vmi_cache: dict[str, dict[str, Any]] = {}
        field_selector = f"metadata.name={settings.target_vm_name}"

        def watched_kind(plural: str, store: dict[str, dict[str, Any]]) -> WatchedKind:
            return WatchedKind(
                name=plural,
                list=vm_api.list_namespaced_custom_object,
                args=("kubevirt.io", "v1", settings.target_namespace, plural),
                kwargs={"field_selector": field_selector, "_request_timeout": (10, WATCH_TIMEOUT_SECONDS + 30)},
                key=lambda obj: str(obj["metadata"]["name"]),
                names=lambda: set(store),
                apply=lambda name, obj: apply_to(store, name, obj),
            )

        async def on_change(_kind: WatchedKind) -> None:
            changed.set()

        async def on_cycle(_kind: WatchedKind, _at: datetime) -> None:
            pass

        informer = ListWatch(
            kinds=(watched_kind("virtualmachines", vm_cache), watched_kind("virtualmachineinstances", vmi_cache)),
            resync_seconds=WATCH_TIMEOUT_SECONDS,
            on_change=on_change,
            on_cycle=on_cycle,
        )
        informer_task = asyncio.create_task(informer.run(), name="kubevirt-vm-informer")
        try:
            await _run_reconcile_loop(vm_api, core, settings, changed, vm_cache=vm_cache, vmi_cache=vmi_cache)
        finally:
            informer_task.cancel()
            await asyncio.gather(informer_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
