"""Sandbox readiness, lifecycle operations, and cleanup diagnostics for system scenarios."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from datetime import datetime
from types import TracebackType
from typing import Any

# gazelle:include_dep @pypi//protobuf
import httpx
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from opentelemetry import trace
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.crd_group import GROUP
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.identity import LAUNCH_DEFAULT_POLICY_SETS
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.vm import boot_rcu
from agentplane.system_tests.vm.launcher_topology import assert_admitted_launcher_topology, is_vm_template
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

POD_TEMPLATE = "environment-tools"
ENVIRONMENT_RUNTIME_CONTAINER = "environment-runtime"
ENVIRONMENT_RUNTIME_LEASE_SUFFIX = "-environment-runtime"
VM_TEMPLATE = "environment-vm"
VM_WORKING_DIRECTORY = "/state/workspaces/accept"
SANDBOX_READY_SECONDS = 300.0
Sandboxes = Callable[..., Coroutine[Any, Any, environment_service_pb2.EnvironmentDescriptor]]


@asynccontextmanager
async def sandbox_scope(cluster: Cluster, client: Client, phase_tracer: trace.Tracer) -> AsyncIterator[Sandboxes]:
    """Own every Sandbox created in this scope, including one that never becomes ready."""
    async with AsyncExitStack() as cleanup:
        owned: list[tuple[str, str]] = []
        failed_vm_readiness: dict[str, tuple[environment_service_pb2.EnvironmentDescriptor, SandboxNotReadyError]] = {}

        async def release_owned(
            _exc_type: type[BaseException] | None, exc: BaseException | None, _traceback: TracebackType | None
        ) -> bool:
            if not owned:
                return False

            async def release_one(name: str, uid: str) -> None:
                failed_readiness = failed_vm_readiness.get(uid)
                if failed_readiness is not None:
                    view, readiness_error = failed_readiness
                    try:
                        path = boot_rcu.capture_readiness_failure(view, undeclared_outputs_dir() / "vm-artifacts")
                        readiness_error.add_note(f"Passive host QEMU readiness snapshot: {path}")
                    except Exception as diagnostic_error:
                        readiness_error.add_note(f"Passive host QEMU readiness snapshot failed: {diagnostic_error!r}")
                await suspend_and_delete(cluster, client, name, uid)

            async def release_all() -> None:
                results = await asyncio.gather(
                    *(release_one(name, uid) for name, uid in reversed(owned)), return_exceptions=True
                )
                failures = [result for result in results if isinstance(result, BaseException)]
                if len(failures) == 1:
                    raise failures[0]
                if failures:
                    raise BaseExceptionGroup("Sandbox cleanup failed", failures)

            closing = asyncio.create_task(release_all())
            cancellation: asyncio.CancelledError | None = None
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError as error:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        cancellation = cancellation or error
                except BaseException:
                    # Read the task result below so all cleanup failures share one path.
                    break

            cleanup_error: BaseException | None = None
            try:
                closing.result()
            except BaseException as error:
                cleanup_error = error

            if cancellation is not None:
                if cleanup_error is not None and cleanup_error is not cancellation:
                    raise cancellation from cleanup_error
                raise cancellation
            if cleanup_error is not None:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc from cleanup_error
                raise cleanup_error
            return False

        launched_identities: list[dict[str, str]] = []

        async def delete_launched_identities() -> None:
            # After every Sandbox is released. Garbage collection removes a deleted Sandbox's
            # WorkloadCredential shortly after, and the delete guard refuses while one acts as it.
            for identity in reversed(launched_identities):
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception_type(httpx.HTTPStatusError),
                    stop=stop_after_delay(60),
                    wait=wait_fixed(2),
                    reraise=True,
                ):
                    with attempt:
                        await client.delete_identity(identity)

        # Registered first, so it runs after release_owned.
        cleanup.push_async_callback(delete_launched_identities)
        cleanup.push_async_exit(release_owned)

        async def create(
            name: str,
            *,
            template: str | None = None,
            policies: list[str] | None = None,
            preset: str | None = None,
            bootstrap: str = "",
            inbox_tags: Sequence[str] = (),
            idle_suspend_after_seconds: int | None = None,
        ) -> environment_service_pb2.EnvironmentDescriptor:
            if template is None:
                template = POD_TEMPLATE
            with phase_tracer.start_as_current_span(
                "system_test.sandbox.create", attributes={"sandbox.name": name, "sandbox.template": template}
            ):
                request = environment_service_pb2.CreateEnvironmentRequest(
                    name=name, template=template, inbox_tags=list(inbox_tags), bootstrap=bootstrap
                )
                if preset is not None:
                    request.preset = preset
                if idle_suspend_after_seconds is not None:
                    request.idle_suspend_after.seconds = idle_suspend_after_seconds
                # The baseline sets a preset grants, beside the scenario's own: these launches use no preset.
                sets = sorted({*LAUNCH_DEFAULT_POLICY_SETS, *(policies or [])})
                created, identity = await client.launch_sandbox(request, sets)
            launched_identities.append(identity)
            # Register the exact acknowledged incarnation before any follow-on watch or Get can fail.
            owned.append((created.name, created.uid))
            try:
                view = await wait_until_running(client, created)
            except SandboxNotReadyError as error:
                failed_view = error.last_view
                if (
                    is_vm_template(template)
                    and failed_view is not None
                    and failed_view.target.namespace == created.namespace
                    and failed_view.target.name == created.name
                    and failed_view.target.uid == created.uid
                ):
                    failed_vm_readiness[created.uid] = (failed_view, error)
                raise
            if is_vm_template(template):
                if not view.kubernetes_diagnostics.HasField("pod"):
                    raise SandboxNotReadyError(
                        f"VM Sandbox {view.target.name} ({view.target.uid}) reached Ready without a launcher Pod identity"
                    )
                await assert_admitted_launcher_topology(
                    cluster, view.target.name, view.target.uid, view.kubernetes_diagnostics.pod
                )
            return view

        yield create


def record_launch_transitions(span: trace.Span, view: environment_service_pb2.EnvironmentDescriptor) -> None:
    """Mark controller and kubelet launch steps before the watch consumer observes Ready."""
    pod_status = (
        MessageToDict(view.kubernetes_diagnostics.pod.status)
        if view.kubernetes_diagnostics.HasField("pod") and view.kubernetes_diagnostics.pod.HasField("status")
        else None
    )
    sandbox_status = (
        MessageToDict(view.kubernetes_diagnostics.sandbox_status)
        if view.kubernetes_diagnostics.HasField("sandbox_status")
        else None
    )
    for source, status in (("sandbox", sandbox_status), ("pod", pod_status)):
        for condition in _dicts((status or {}).get("conditions")):
            at = condition.get("lastTransitionTime")
            if condition.get("status") == "True" and isinstance(at, str):
                span.add_event(f"{source}.{condition.get('type')}", timestamp=_nanoseconds(at))
    for container in _dicts((pod_status or {}).get("containerStatuses")):
        state = container.get("state")
        running = state.get("running") if isinstance(state, dict) else None
        started = running.get("startedAt") if isinstance(running, dict) else None
        if isinstance(started, str):
            span.add_event(
                "pod.container_started", {"container": str(container.get("name"))}, timestamp=_nanoseconds(started)
            )


def _dicts(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _nanoseconds(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp).timestamp() * 1e9)


class SandboxNotReadyError(Exception):
    """A lifecycle wait ended before the expected Sandbox state was observed."""

    def __init__(self, message: str, *, last_view: environment_service_pb2.EnvironmentDescriptor | None = None) -> None:
        super().__init__(message)
        self.last_view = last_view


async def wait_until_running(
    client: Client, target: identity_pb2.EnvironmentTarget
) -> environment_service_pb2.EnvironmentDescriptor:
    """Wait for the named Sandbox incarnation to report its own Ready condition."""
    if not target.namespace or not target.name or not target.uid:
        raise ValueError("complete EnvironmentService target handle is required")
    requested_target = identity_pb2.EnvironmentTarget()
    requested_target.CopyFrom(target)
    name, expected_uid = requested_target.name, requested_target.uid
    with tracer.start_as_current_span(
        "system_test.sandbox.ready", attributes={"sandbox.name": name, "sandbox.uid": expected_uid}
    ) as ready_span:
        deadline = asyncio.timeout(SANDBOX_READY_SECONDS)
        last_status: dict[str, object] | None = None
        last_view: environment_service_pb2.EnvironmentDescriptor | None = None
        try:
            async with deadline, aclosing(client.sandbox_snapshots()) as snapshots:
                async for snapshot in snapshots:
                    candidates = [
                        row
                        for row in snapshot
                        if row.target.name == name and row.target.namespace == requested_target.namespace
                    ]
                    observed = next((row for row in candidates if row.target.uid == expected_uid), None)
                    if observed is None:
                        if candidates:
                            raise SandboxNotReadyError(
                                f"{name} has been replaced: expected {expected_uid}, "
                                f"observed {', '.join(row.target.uid for row in candidates)}",
                                last_view=last_view,
                            )
                        continue
                    view = observed
                    last_view = view
                    last_status = (
                        MessageToDict(view.kubernetes_diagnostics.sandbox_status)
                        if view.kubernetes_diagnostics.HasField("sandbox_status")
                        else {}
                    )
                    if (
                        view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
                        and view.observed_state.HasField("readiness")
                        and view.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY
                    ):
                        record_launch_transitions(ready_span, view)
                        return view
                raise SandboxNotReadyError(
                    f"{name}'s Sandbox inventory stream ended before observing Ready", last_view=last_view
                )
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise SandboxNotReadyError(
                f"{name} did not reach Ready within {SANDBOX_READY_SECONDS}s: {last_status}", last_view=last_view
            ) from error


async def suspend_and_delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
    with tracer.start_as_current_span(
        "system_test.sandbox.cleanup", attributes={"sandbox.name": name, "sandbox.uid": uid}
    ) as cleanup:
        target = identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name=name, uid=uid)
        await client.suspend_sandbox(target)
        cleanup.add_event("suspend.accepted")
        # Deletion admission reads effective mode, which the controller publishes after the spec write.
        delete_requested = False
        last_snapshot = None
        try:
            async with asyncio.timeout(SANDBOX_READY_SECONDS), aclosing(client.sandbox_snapshots()) as snapshots:
                async for snapshot in snapshots:
                    last_snapshot = snapshot
                    view = next(
                        (
                            view
                            for view in snapshot
                            if view.target.namespace == target.namespace and view.target.name == name
                        ),
                        None,
                    )
                    if view is None:
                        if delete_requested:
                            # A removed watch item may also mean the caller lost visibility. Verify
                            # Kubernetes has removed this exact incarnation before reusing its name.
                            await _wait_for_sandbox_deletion(cluster, name, uid)
                            cleanup.add_event("deleted")
                            return
                        continue
                    if view.target.uid != uid:
                        raise SandboxNotReadyError(
                            f"{name} has been replaced: expected {uid}, observed {view.target.uid}"
                        )
                    if (
                        not delete_requested
                        and view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
                    ):
                        cleanup.add_event("suspended")
                        await client.delete_sandbox(target)
                        cleanup.add_event("delete.accepted")
                        # The delete response only accepts the request. Keep controllers and kubelet alive
                        # until the finalizer has removed the VM/Pod and released its volumes.
                        delete_requested = True
                raise SandboxNotReadyError(f"{name}'s stream ended before observing deletion")
        except TimeoutError as error:
            try:
                path = await _write_cleanup_diagnostics(cluster, client, name, uid, last_snapshot)
                error.add_note(f"Sandbox cleanup diagnostics: {path}")
            except Exception as diagnostic_error:
                error.add_note(f"Sandbox cleanup diagnostics failed: {diagnostic_error!r}")
            raise


async def _wait_for_sandbox_deletion(cluster: Cluster, name: str, uid: str) -> None:
    while True:
        raw = await cluster.kubectl("get", "sandboxes", name, "--ignore-not-found", "-o", "json")
        if not raw:
            return
        observed_uid = json.loads(raw)["metadata"]["uid"]
        if observed_uid != uid:
            raise SandboxNotReadyError(f"{name} has been replaced: expected {uid}, observed {observed_uid}")
        await asyncio.sleep(0.25)


async def _write_cleanup_diagnostics(cluster: Cluster, client: Client, name: str, uid: str, last_snapshot: Any) -> str:
    """Capture the state and control logs when the effective suspend watch stops progressing."""

    diagnostic: dict[str, Any] = {
        "sandbox_name": name,
        "last_live_snapshot": None if last_snapshot is None else [MessageToDict(row) for row in last_snapshot],
    }

    async def app_state(key: str, operation: Callable[[], Coroutine[Any, Any, Message]]) -> None:
        try:
            async with asyncio.timeout(10):
                value = await operation()
                diagnostic[key] = MessageToDict(
                    value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
                )
        except Exception as error:
            diagnostic[f"{key}_error"] = repr(error)

    observed = next(
        (
            row
            for row in last_snapshot or []
            if row.target.namespace == cluster.namespace and row.target.name == name and row.target.uid == uid
        ),
        None,
    )
    state_operations = [app_state("runner_status", lambda: client.runner_status(name, uid))]
    if observed is not None:
        state_operations.append(app_state("app_sandbox", lambda: client.get_environment(observed.target)))
    await asyncio.gather(*state_operations)

    async def kubectl(*args: str) -> dict[str, Any]:
        try:
            return {"returncode": 0, "stdout": await cluster.kubectl(*args, timeout_s=10)}
        except Exception as error:
            return {"error": repr(error)}

    resource, lease, pods, controller_logs, gateway_logs = await asyncio.gather(
        kubectl("get", "sandbox", name, "-o", "json"),
        kubectl("get", "lease", f"{name}{ENVIRONMENT_RUNTIME_LEASE_SUFFIX}", "-o", "json"),
        kubectl("get", "pods", "-l", f"{GROUP}/sandbox={name}", "-o", "json"),
        kubectl("logs", "deployment/sandbox-controller", "--all-pods=true", "--tail=100", "--since=10m"),
        kubectl("logs", "deployment/runner-gateway", "--all-pods=true", "--tail=100", "--since=10m"),
    )
    diagnostic["kubernetes_sandbox"] = resource
    diagnostic["environment_runtime_lease"] = lease
    diagnostic["sandbox_pods"] = pods
    diagnostic["sandbox_controller_logs"] = controller_logs
    diagnostic["runner_gateway_logs"] = gateway_logs
    if pods.get("returncode") == 0:
        try:
            pod_names = [pod["metadata"]["name"] for pod in json.loads(pods["stdout"]).get("items", [])]
        except KeyError, TypeError, ValueError:
            pod_names = []
        diagnostic["environment_runtime_logs"] = await asyncio.gather(
            *(
                kubectl("logs", f"pod/{pod}", "-c", ENVIRONMENT_RUNTIME_CONTAINER, "--tail=100", "--since=10m")
                for pod in pod_names
            )
        )
    path = undeclared_outputs_dir() / f"sandbox-cleanup-{name}.json"
    path.write_text(json.dumps(diagnostic, indent=2) + "\n")
    return str(path)
