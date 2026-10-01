"""Restart a configured KubeVirt VMI after Flux stages a VM template update.

The configured VM is stateless and uses `runStrategy: Always`. KubeVirt marks
non-live-updatable template changes with `RestartRequired`, but leaves the old VMI
running. This controller watches one configured, explicitly opted-in VM by polling its
namespaced API objects. It stores rollout progress in a dedicated ConfigMap so a pod
restart cannot repeat a restart indefinitely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

NAMESPACE = os.environ["TARGET_NAMESPACE"]
TARGET_VM_NAME = os.environ["TARGET_VM_NAME"]
STATE_NAME = os.environ["STATE_CONFIGMAP_NAME"]
CONTROLLER = "vm-image-restart"
OPT_IN_ANNOTATION = "ducktape.org/auto-restart-template-changes"
STATE_KEY = "state"
POLL_SECONDS = 15
MAX_RESTART_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (15, 60, 180)
ROLLOUT_TIMEOUT = timedelta(minutes=30)
RESTART_GRACE_SECONDS = 60

logger = logging.getLogger(CONTROLLER)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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


async def _get_vmi(api: client.CustomObjectsApi) -> dict[str, Any] | None:
    try:
        return cast(
            dict[str, Any],
            await api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=NAMESPACE,
                plural="virtualmachineinstances",
                name=TARGET_VM_NAME,
            ),
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


async def _read_state(core: client.CoreV1Api) -> dict[str, Any] | None:
    try:
        config_map = await core.read_namespaced_config_map(name=STATE_NAME, namespace=NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            raise RuntimeError(f"required state ConfigMap {NAMESPACE}/{STATE_NAME} is missing") from exc
        raise
    raw = (config_map.data or {}).get(STATE_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("state must be a JSON object")
        return state
    except json.JSONDecodeError, ValueError:
        logger.exception("ignoring malformed restart state in %s/%s", NAMESPACE, STATE_NAME)
        return None


async def _write_state(core: client.CoreV1Api, state: dict[str, Any]) -> None:
    # Merge-patch only our `data.state` key; Flux owns the ConfigMap object and its metadata.
    await core.patch_namespaced_config_map(
        name=STATE_NAME,
        namespace=NAMESPACE,
        body={"data": {STATE_KEY: json.dumps(state, sort_keys=True, separators=(",", ":"))}},
    )


async def _event(
    core: client.CoreV1Api, vm: dict[str, Any], *, reason: str, message: str, event_type: str = "Normal"
) -> None:
    now = _utcnow()
    event = client.CoreV1Event(
        metadata=client.V1ObjectMeta(generate_name=f"{TARGET_VM_NAME}-", namespace=NAMESPACE),
        involved_object=client.V1ObjectReference(
            api_version="kubevirt.io/v1",
            kind="VirtualMachine",
            name=TARGET_VM_NAME,
            namespace=NAMESPACE,
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
        await core.create_namespaced_event(namespace=NAMESPACE, body=event)
    except ApiException:
        logger.exception("could not publish %s event for %s/%s", reason, NAMESPACE, TARGET_VM_NAME)


def _new_state(generation: int, image: str, vmi_uid: str | None) -> dict[str, Any]:
    now = _utcnow()
    return {
        "generation": generation,
        "image": image,
        "oldVmiUid": vmi_uid,
        "attempts": 0,
        "phase": "Restarting",
        "startedAt": now.isoformat(),
        "nextAttemptAt": now.isoformat(),
        "message": "waiting for the new VMI",
    }


async def _finish(
    *, core: client.CoreV1Api, vm: dict[str, Any], state: dict[str, Any], phase: str, message: str, reason: str
) -> None:
    state.update(phase=phase, message=message, finishedAt=_utcnow().isoformat())
    await _write_state(core, state)
    await _event(core, vm, reason=reason, message=message, event_type="Warning" if phase == "Failed" else "Normal")


async def _request_restart(
    *,
    vm_api: client.CustomObjectsApi,
    core: client.CoreV1Api,
    vm: dict[str, Any],
    state: dict[str, Any],
    vmi: dict[str, Any],
) -> None:
    attempts = int(state.get("attempts", 0))
    if attempts >= MAX_RESTART_ATTEMPTS:
        await _finish(
            core=core,
            vm=vm,
            state=state,
            phase="Failed",
            reason="VMImageRestartFailed",
            message=f"gave up after {attempts} VMI restart requests; the desired image is {state['image']}",
        )
        return

    now = _utcnow()
    next_attempt = _timestamp(state.get("nextAttemptAt", now.isoformat()))
    if now < next_attempt:
        return
    attempts += 1
    backoff = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    state.update(
        attempts=attempts,
        oldVmiUid=vmi.get("metadata", {}).get("uid"),
        nextAttemptAt=(now + timedelta(seconds=backoff)).isoformat(),
        message=f"requested graceful VMI restart {attempts}/{MAX_RESTART_ATTEMPTS}",
    )
    # Persist before deleting so a controller pod restart cannot issue an unbounded series
    # of restart requests while KubeVirt is still terminating the old VMI.
    await _write_state(core, state)
    try:
        await vm_api.delete_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=NAMESPACE,
            plural="virtualmachineinstances",
            name=TARGET_VM_NAME,
            body=client.V1DeleteOptions(grace_period_seconds=RESTART_GRACE_SECONDS),
        )
    except ApiException as exc:
        state["message"] = f"restart request {attempts}/{MAX_RESTART_ATTEMPTS} failed: {exc.reason or exc.status}"
        await _write_state(core, state)
        logger.warning("VMI restart request failed: %s", exc)
        if attempts == MAX_RESTART_ATTEMPTS:
            await _finish(
                core=core, vm=vm, state=state, phase="Failed", reason="VMImageRestartFailed", message=state["message"]
            )
        else:
            await _event(core, vm, reason="VMImageRestartRetrying", message=state["message"], event_type="Warning")
        return

    message = f"requested graceful VMI restart for template generation {state['generation']} ({state['image']})"
    state["message"] = message
    await _write_state(core, state)
    await _event(core, vm, reason="VMImageRestartRequested", message=message)


async def reconcile(vm_api: client.CustomObjectsApi, core: client.CoreV1Api) -> None:
    vm = await vm_api.get_namespaced_custom_object(
        group="kubevirt.io", version="v1", namespace=NAMESPACE, plural="virtualmachines", name=TARGET_VM_NAME
    )
    annotations = vm.get("metadata", {}).get("annotations", {})
    if annotations.get(OPT_IN_ANNOTATION) != "true":
        return

    generation = int(vm.get("status", {}).get("desiredGeneration") or vm["metadata"].get("generation", 0))
    observed_generation = int(vm.get("status", {}).get("observedGeneration", 0))
    desired_image = _container_disk_image(vm.get("spec", {}).get("template", {}).get("spec", {}))
    if not desired_image:
        logger.error("%s/%s has no rootdisk containerDisk image", NAMESPACE, TARGET_VM_NAME)
        return

    vmi = await _get_vmi(vm_api)
    state = await _read_state(core)
    now = _utcnow()

    if state and int(state.get("generation", -1)) != generation:
        if state.get("phase") in {"Ready", "Failed"}:
            state = None
        else:
            # A newer Flux update superseded an in-progress rollout. Keep the original VMI UID
            # so the controller waits for the replacement already underway, but verify the
            # replacement against the latest template.
            state.update(
                generation=generation,
                image=desired_image,
                attempts=0,
                startedAt=now.isoformat(),
                nextAttemptAt=now.isoformat(),
                message="newer VM template superseded the in-progress restart",
            )
            await _write_state(core, state)

    vmi_uid = (vmi or {}).get("metadata", {}).get("uid")
    vmi_image = _container_disk_image((vmi or {}).get("spec", {}))
    restart_required = _restart_required(vm)

    if state is None:
        if not restart_required or (observed_generation >= generation and vmi_image == desired_image):
            return
        state = _new_state(generation, desired_image, vmi_uid)
        await _write_state(core, state)
        if vmi is None:
            return

    if state.get("phase") == "Ready":
        return
    if state.get("phase") == "Failed":
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
                vm=vm,
                state=state,
                phase="Ready",
                reason="VMImageRestartRecovered",
                message=f"KubeVirt recovered the VMI on template generation {generation} ({desired_image})",
            )
        return

    started_at = _timestamp(state.get("startedAt", now.isoformat()))
    if now - started_at >= ROLLOUT_TIMEOUT:
        await _finish(
            core=core,
            vm=vm,
            state=state,
            phase="Failed",
            reason="VMImageRestartTimedOut",
            message=(
                f"VMI did not become Ready on image {state['image']} within "
                f"{int(ROLLOUT_TIMEOUT.total_seconds() // 60)} minutes"
            ),
        )
        return

    if vmi is None:
        return

    old_uid = state.get("oldVmiUid")
    if old_uid is None:
        state["oldVmiUid"] = vmi_uid
        if vmi_image == state["image"]:
            # The VMI appeared after the controller began watching. Let a matching new image
            # finish booting before considering another restart for any remaining staged fields.
            state["nextAttemptAt"] = (now + timedelta(seconds=60)).isoformat()
        await _write_state(core, state)
        old_uid = vmi_uid

    if vmi_uid != old_uid:
        if vmi_image == state["image"]:
            if _ready(vmi) and observed_generation >= generation and not restart_required:
                await _finish(
                    core=core,
                    vm=vm,
                    state=state,
                    phase="Ready",
                    reason="VMImageRestartReady",
                    message=f"replacement VMI is Ready on template generation {generation} ({state['image']})",
                )
            else:
                state.update(oldVmiUid=vmi_uid, nextAttemptAt=(now + timedelta(seconds=60)).isoformat())
                await _write_state(core, state)
            return
        # KubeVirt recreated a VMI but it still reflects the old template. A further
        # graceful restart is allowed only within the same bounded attempt budget.
        state.update(oldVmiUid=vmi_uid, nextAttemptAt=now.isoformat())
        await _write_state(core, state)
        await _request_restart(vm_api=vm_api, core=core, vm=vm, state=state, vmi=vmi)
        return

    if _ready(vmi) and observed_generation >= generation and vmi_image == state["image"] and not restart_required:
        await _finish(
            core=core,
            vm=vm,
            state=state,
            phase="Ready",
            reason="VMImageRestartReady",
            message=f"VMI is Ready on template generation {generation} ({state['image']})",
        )
        return

    if vmi_image == state["image"] and not _ready(vmi):
        # A VMI already using the new image can still be pulling or booting. `Always` handles
        # retrying failed launches; restarting it while it is coming up would create a loop.
        return

    if vmi.get("metadata", {}).get("deletionTimestamp"):
        return
    if restart_required and (observed_generation < generation or vmi_image != state["image"]):
        await _request_restart(vm_api=vm_api, core=core, vm=vm, state=state, vmi=vmi)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await config.load_incluster_config()
    async with client.ApiClient() as api_client:
        vm_api = client.CustomObjectsApi(api_client)
        core = client.CoreV1Api(api_client)
        while True:
            try:
                await reconcile(vm_api, core)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reconciliation failed for %s/%s", NAMESPACE, TARGET_VM_NAME)
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
