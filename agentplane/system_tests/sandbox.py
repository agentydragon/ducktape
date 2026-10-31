"""Sandbox readiness, lifecycle operations, and cleanup diagnostics for system scenarios."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.app.testing.models import NewSandbox, SandboxView, sandbox_is_ready
from agentplane.crd_group import GROUP
from agentplane.sandbox_resource import OperatingMode
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.tracing import tracer
from util.testing.undeclared_outputs import undeclared_outputs_dir

POD_TEMPLATE = "runner"
VM_TEMPLATE = "runner-vm"
VM_WORKING_DIRECTORY = "/state/workspaces/accept"
SANDBOX_READY_SECONDS = 300.0
Sandboxes = Callable[..., Coroutine[Any, Any, SandboxView]]


@asynccontextmanager
async def sandbox_scope(cluster: Cluster, client: Client, phase_tracer: trace.Tracer) -> AsyncIterator[Sandboxes]:
    """Own every Sandbox created in this scope, including one that never becomes ready."""
    async with AsyncExitStack() as cleanup:
        owned: list[tuple[str, UUID]] = []

        async def release_owned(
            _exc_type: type[BaseException] | None, exc: BaseException | None, _traceback: TracebackType | None
        ) -> bool:
            if not owned:
                return False

            async def release_all() -> None:
                results = await asyncio.gather(
                    *(suspend_and_delete(cluster, client, name, uid) for name, uid in reversed(owned)),
                    return_exceptions=True,
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

        cleanup.push_async_exit(release_owned)

        async def create(
            name: str,
            *,
            template: str | None = None,
            policies: list[str] | None = None,
            preset: str | None = None,
            bootstrap: str = "",
            inbox_tags: Sequence[str] = (),
        ) -> SandboxView:
            if template is None:
                template = POD_TEMPLATE
            with phase_tracer.start_as_current_span(
                "system_test.sandbox.create", attributes={"sandbox.name": name, "sandbox.template": template}
            ):
                view = await client.create_sandbox(
                    NewSandbox(
                        name=name,
                        template=template,
                        preset=preset,
                        policy_sets=policies or [],
                        inbox_tags=list(inbox_tags),
                        bootstrap=bootstrap,
                    )
                )
            # Register before readiness so failed boots are released too.
            owned.append((view.name, view.uid))
            with phase_tracer.start_as_current_span(
                "system_test.sandbox.ready", attributes={"sandbox.name": view.name, "sandbox.uid": str(view.uid)}
            ) as ready_span:
                ready = await wait_until_running(client, view.name, expected_uid=view.uid)
                record_launch_transitions(ready_span, ready)
                return ready

        yield create


def record_launch_transitions(span: trace.Span, view: SandboxView) -> None:
    """Mark controller and kubelet launch steps before the watch consumer observes Ready."""
    pod_status = view.pod.status if view.pod else None
    for source, status in (("sandbox", view.status), ("pod", pod_status)):
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


async def wait_until_running(client: Client, name: str, *, expected_uid: UUID) -> SandboxView:
    """Wait for the named Sandbox incarnation to report its own Ready condition."""
    deadline = asyncio.timeout(SANDBOX_READY_SECONDS)
    last_status: dict[str, object] | None = None
    try:
        async with deadline, aclosing(client.sandbox_snapshots()) as snapshots:
            async for snapshot in snapshots:
                if not snapshot.watch.healthy:
                    continue
                view = next((view for view in snapshot.sandboxes if view.name == name), None)
                if view is None:
                    continue
                if view.uid != expected_uid:
                    raise SandboxNotReadyError(
                        f"{name} has been replaced: expected {expected_uid}, observed {view.uid}"
                    )
                last_status = view.status
                if sandbox_is_ready(view):
                    return view
            raise SandboxNotReadyError(f"{name}'s Sandbox inventory stream ended before observing Ready")
    except TimeoutError as error:
        if not deadline.expired():
            raise
        raise SandboxNotReadyError(
            f"{name} did not reach Ready within {SANDBOX_READY_SECONDS}s: {last_status}"
        ) from error


async def suspend_and_delete(cluster: Cluster, client: Client, name: str, uid: UUID) -> None:
    with tracer.start_as_current_span(
        "system_test.sandbox.cleanup", attributes={"sandbox.name": name, "sandbox.uid": str(uid)}
    ) as cleanup:
        await client.suspend_sandbox(name)
        cleanup.add_event("suspend.accepted")
        # Deletion admission reads effective mode, which the controller publishes after the spec write.
        delete_requested = False
        last_snapshot = None
        try:
            async with asyncio.timeout(SANDBOX_READY_SECONDS), aclosing(client.sandbox_snapshots()) as snapshots:
                async for snapshot in snapshots:
                    last_snapshot = snapshot
                    if not snapshot.watch.healthy:
                        continue
                    view = next((view for view in snapshot.sandboxes if view.name == name), None)
                    if view is None:
                        if delete_requested:
                            cleanup.add_event("deleted")
                            return
                        continue
                    if view.uid != uid:
                        raise SandboxNotReadyError(f"{name} has been replaced: expected {uid}, observed {view.uid}")
                    if not delete_requested and view.operating_mode is OperatingMode.SUSPENDED:
                        cleanup.add_event("suspended")
                        await client.delete_sandbox(name)
                        cleanup.add_event("delete.accepted")
                        # The delete response only accepts the request. Keep controllers and kubelet alive
                        # until the finalizer has removed the VM/Pod and released its volumes.
                        delete_requested = True
                raise SandboxNotReadyError(f"{name}'s stream ended before observing deletion")
        except TimeoutError as error:
            try:
                path = await _write_cleanup_diagnostics(cluster, client, name, last_snapshot)
                error.add_note(f"Sandbox cleanup diagnostics: {path}")
            except Exception as diagnostic_error:
                error.add_note(f"Sandbox cleanup diagnostics failed: {diagnostic_error!r}")
            raise


async def _write_cleanup_diagnostics(cluster: Cluster, client: Client, name: str, last_snapshot: Any) -> str:
    """Capture the state and control logs when the effective suspend watch stops progressing."""

    diagnostic: dict[str, Any] = {
        "sandbox_name": name,
        "last_live_snapshot": None if last_snapshot is None else last_snapshot.model_dump(mode="json"),
    }

    async def app_state(key: str, operation: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        try:
            async with asyncio.timeout(10):
                diagnostic[key] = (await operation()).model_dump(mode="json")
        except Exception as error:
            diagnostic[f"{key}_error"] = repr(error)

    await asyncio.gather(
        app_state("app_sandbox", lambda: client.sandbox(name)),
        app_state("runner_status", lambda: client.runner_status(name)),
    )

    async def kubectl(*args: str) -> dict[str, Any]:
        try:
            return {"returncode": 0, "stdout": await cluster.kubectl(*args, timeout_s=10)}
        except Exception as error:
            return {"error": repr(error)}

    resource, lease, pods, controller_logs, gateway_logs = await asyncio.gather(
        kubectl("get", "sandbox", name, "-o", "json"),
        kubectl("get", "lease", f"{name}-runner", "-o", "json"),
        kubectl("get", "pods", "-l", f"{GROUP}/sandbox={name}", "-o", "json"),
        kubectl("logs", "deployment/sandbox-controller", "--all-pods=true", "--tail=100", "--since=10m"),
        kubectl("logs", "deployment/runner-gateway", "--all-pods=true", "--tail=100", "--since=10m"),
    )
    diagnostic["kubernetes_sandbox"] = resource
    diagnostic["runner_lease"] = lease
    diagnostic["sandbox_pods"] = pods
    diagnostic["sandbox_controller_logs"] = controller_logs
    diagnostic["runner_gateway_logs"] = gateway_logs
    if pods.get("returncode") == 0:
        try:
            pod_names = [pod["metadata"]["name"] for pod in json.loads(pods["stdout"]).get("items", [])]
        except KeyError, TypeError, ValueError:
            pod_names = []
        diagnostic["runner_logs"] = await asyncio.gather(
            *(kubectl("logs", f"pod/{pod}", "-c", "runner", "--tail=100", "--since=10m") for pod in pod_names)
        )
    path = undeclared_outputs_dir() / f"sandbox-cleanup-{name}.json"
    path.write_text(json.dumps(diagnostic, indent=2) + "\n")
    return str(path)
