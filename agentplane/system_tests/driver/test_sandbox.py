"""Sandbox lifecycle helpers wait for authoritative state and bound cleanup."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock, call, create_autospec
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2

# gazelle:include_dep @pypi//protobuf
from agentplane.system_tests.driver import sandbox
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import SandboxNotReadyError, suspend_and_delete


def _environment(
    name: str,
    uid: str,
    *,
    ready: bool,
    operating_mode: str = "OPERATING_MODE_RUNNING",
    namespace: str = "test",
    pod_uid: str | None = None,
) -> environment_service_pb2.EnvironmentDescriptor:
    view = environment_service_pb2.EnvironmentDescriptor(
        target=identity_pb2.EnvironmentTarget(namespace=namespace, name=name, uid=uid),
        execution_account=identity_pb2.ServiceAccountIdentity(
            account=identity_pb2.ServiceAccount(namespace="test", name="runner")
        ),
        observed_state=environment_service_pb2.EnvironmentObservedState(
            operating_mode=operating_mode,
            readiness=environment_service_pb2.Readiness(
                state=environment_service_pb2.READINESS_STATE_READY
                if ready
                else environment_service_pb2.READINESS_STATE_NOT_READY
            ),
        ),
    )
    if pod_uid is not None:
        view.kubernetes_diagnostics.pod.CopyFrom(
            environment_service_pb2.SandboxPod(name=f"virt-launcher-{name}", namespace=namespace, uid=pod_uid)
        )
    return view


def _snapshot(
    *environments: environment_service_pb2.EnvironmentDescriptor,
) -> list[environment_service_pb2.EnvironmentDescriptor]:
    return list(environments)


def _target(name: str, uid: str) -> identity_pb2.EnvironmentTarget:
    return identity_pb2.EnvironmentTarget(namespace="test", name=name, uid=uid)


def _create_targets(client: Client, names: tuple[str, ...]) -> dict[str, identity_pb2.EnvironmentTarget]:
    created = {name: _target(name, str(uuid4())) for name in names}
    cast(AsyncMock, client.create_sandbox).side_effect = list(created.values())
    return created


def _ready_for_targets(
    views: dict[str, identity_pb2.EnvironmentTarget],
) -> Callable[[Client, identity_pb2.EnvironmentTarget], Awaitable[environment_service_pb2.EnvironmentDescriptor]]:
    async def ready(
        _client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == views[target.name]
        return _environment(target.name, target.uid, ready=True)

    return ready


@pytest.fixture
def cluster() -> Cluster:
    access = create_autospec(Cluster, instance=True)
    access.namespace = "test"
    access.kubectl.side_effect = RuntimeError("test diagnostics unavailable")
    return cast(Cluster, access)


async def test_readiness_accepts_an_already_ready_sandbox_from_initial_inventory() -> None:
    client = create_autospec(Client, instance=True)
    view = _environment("readiness-test", str(uuid4()), ready=True)
    closed = False

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        nonlocal closed
        try:
            yield _snapshot(view)
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()

    assert await sandbox.wait_until_running(client, view.target) is view

    client.sandbox_snapshots.assert_called_once_with()
    assert closed


async def test_readiness_waits_for_a_later_ready_snapshot() -> None:
    client = create_autospec(Client, instance=True)
    uid = str(uuid4())
    ready = _environment("readiness-test", uid, ready=True)
    not_ready = _environment("readiness-test", uid, ready=False)
    suspended_ready_condition = _environment(
        "readiness-test", uid, ready=True, operating_mode="OPERATING_MODE_SUSPENDED"
    )

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield _snapshot(_environment("readiness-test", uid, ready=True, namespace="other-test-namespace"))
        yield _snapshot(suspended_ready_condition)
        yield _snapshot(not_ready)
        yield _snapshot(ready)

    client.sandbox_snapshots.return_value = snapshots()

    assert await sandbox.wait_until_running(client, ready.target) is ready


async def test_readiness_propagates_a_lost_inventory_source() -> None:
    client = create_autospec(Client, instance=True)

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield []
        raise RuntimeError("environment watch source failed")

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(RuntimeError, match="source failed"):
        await sandbox.wait_until_running(client, _target("readiness-test", str(uuid4())))


async def test_readiness_rejects_a_replaced_sandbox_incarnation() -> None:
    client = create_autospec(Client, instance=True)
    expected_uid = str(uuid4())
    replacement = _environment("readiness-test", str(uuid4()), ready=True)
    expected = identity_pb2.EnvironmentTarget(namespace="test", name="readiness-test", uid=expected_uid)

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield _snapshot(replacement)

    client.sandbox_snapshots.return_value = snapshots()

    with pytest.raises(SandboxNotReadyError, match="has been replaced"):
        await sandbox.wait_until_running(client, expected)


async def test_readiness_timeout_closes_the_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_READY_SECONDS", 0.01)
    client = create_autospec(Client, instance=True)
    view = _environment("readiness-test", str(uuid4()), ready=False)
    closed = False

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        nonlocal closed
        try:
            yield _snapshot(view)
            await asyncio.Event().wait()
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()

    with pytest.raises(SandboxNotReadyError) as raised:
        await sandbox.wait_until_running(client, view.target)

    assert isinstance(raised.value.__cause__, TimeoutError)
    assert raised.value.last_view is view
    assert closed


async def test_readiness_propagates_inventory_stream_end() -> None:
    client = create_autospec(Client, instance=True)
    uid = str(uuid4())

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield _snapshot()
        raise RuntimeError("the Sandbox inventory stream ended")
        yield  # pragma: no cover

    client.sandbox_snapshots.return_value = snapshots()

    with pytest.raises(RuntimeError, match="inventory stream ended"):
        await sandbox.wait_until_running(client, _target("readiness-test", uid))


async def test_cleanup_waits_for_effective_suspension_and_actual_deletion(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)
    uid = str(uuid4())
    closed = False
    deletion_observed = False

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        nonlocal closed, deletion_observed
        try:
            for name, namespace, mode in [
                ("owned-test-sandbox", "test", "OPERATING_MODE_RUNNING"),
                ("owned-test-sandbox", "other-test-namespace", "OPERATING_MODE_SUSPENDED"),
                ("owned-test-sandbox", "test", "OPERATING_MODE_SUSPENDED"),
            ]:
                client.delete_sandbox.assert_not_awaited()
                yield [_environment(name, uid, ready=False, operating_mode=mode, namespace=namespace)]
            client.delete_sandbox.assert_awaited_once_with(
                identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name="owned-test-sandbox", uid=uid)
            )
            deletion_observed = True
            yield []
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()
    # The caller-scoped Environment watch can remove an item before Kubernetes finishes deletion.
    cast(AsyncMock, cluster.kubectl).side_effect = [json.dumps({"metadata": {"uid": uid}}), ""]

    await suspend_and_delete(cluster, client, "owned-test-sandbox", uid)
    assert cast(AsyncMock, cluster.kubectl).await_count == 2

    assert client.mock_calls == [
        call.suspend_sandbox(
            identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name="owned-test-sandbox", uid=uid)
        ),
        call.sandbox_snapshots(),
        call.delete_sandbox(
            identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name="owned-test-sandbox", uid=uid)
        ),
    ]
    assert closed
    assert deletion_observed


async def test_cleanup_fences_replacement_after_inventory_removal(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)
    uid = str(uuid4())

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield [_environment("owned-test-sandbox", uid, ready=False, operating_mode="OPERATING_MODE_SUSPENDED")]
        yield []

    client.sandbox_snapshots.return_value = snapshots()
    cast(AsyncMock, cluster.kubectl).side_effect = [json.dumps({"metadata": {"uid": str(uuid4())}})]
    with pytest.raises(SandboxNotReadyError, match="has been replaced"):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", uid)
    client.delete_sandbox.assert_awaited_once_with(_target("owned-test-sandbox", uid))


async def test_cleanup_does_not_delete_a_replacement_sandbox(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        yield [_environment("owned-test-sandbox", str(uuid4()), ready=False, operating_mode="OPERATING_MODE_SUSPENDED")]

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(SandboxNotReadyError, match="has been replaced"):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", str(uuid4()))
    client.delete_sandbox.assert_not_awaited()


async def test_cleanup_bounds_a_watch_that_stops_making_progress(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_READY_SECONDS", 0.01)
    client = create_autospec(Client, instance=True)
    closed = False

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        nonlocal closed
        try:
            yield []
            await asyncio.Event().wait()
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(TimeoutError):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", str(uuid4()))
    client.delete_sandbox.assert_not_awaited()
    assert closed


async def test_cleanup_does_not_retry_an_authorization_refusal_or_delete(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)
    request = httpx.Request("GET", "https://app.test.invalid/sandboxes/owned-test-sandbox")
    refused = httpx.HTTPStatusError("read refused", request=request, response=httpx.Response(403, request=request))

    async def snapshots() -> AsyncIterator[list[environment_service_pb2.EnvironmentDescriptor]]:
        raise refused
        yield  # pragma: no cover

    client.sandbox_snapshots.return_value = snapshots()

    with pytest.raises(httpx.HTTPStatusError):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", str(uuid4()))

    client.sandbox_snapshots.assert_called_once()
    client.delete_sandbox.assert_not_awaited()


async def test_scope_releases_owned_sandboxes_concurrently(cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    client = create_autospec(Client, instance=True)
    views = _create_targets(client, ("owned-0", "owned-1"))

    monkeypatch.setattr(sandbox, "wait_until_running", _ready_for_targets(views))
    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()
    finished: set[str] = set()

    async def delete(_cluster: Cluster, _client: Client, name: str, _uid: object) -> None:
        started.add(name)
        if started == set(views):
            all_started.set()
        await release.wait()
        finished.add(name)

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.get_tracer(__name__)) as create:
            await create("owned-0")
            await create("owned-1")

    closing = asyncio.create_task(use_scope())
    try:
        await asyncio.wait_for(all_started.wait(), timeout=5)
    except TimeoutError:
        release.set()
        await closing
        raise
    release.set()
    await closing
    assert started == set(views)
    assert finished == set(views)


async def test_scope_drains_other_owners_when_one_cleanup_fails(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    views = _create_targets(client, ("owned-0", "owned-1"))

    monkeypatch.setattr(sandbox, "wait_until_running", _ready_for_targets(views))
    started: set[str] = set()
    peer_started = asyncio.Event()
    release_peer = asyncio.Event()
    failed_owner = asyncio.Event()
    finished: set[str] = set()

    async def delete(_cluster: Cluster, _client: Client, name: str, _uid: object) -> None:
        started.add(name)
        if name == "owned-1":
            await peer_started.wait()
            failed_owner.set()
            raise RuntimeError("test cleanup failure")
        peer_started.set()
        await release_peer.wait()
        finished.add(name)

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.get_tracer(__name__)) as create:
            await create("owned-0")
            await create("owned-1")

    closing = asyncio.create_task(use_scope())
    try:
        await asyncio.wait_for(failed_owner.wait(), timeout=5)
    except TimeoutError:
        release_peer.set()
        await closing
        raise
    assert not closing.done()
    release_peer.set()
    with pytest.raises(RuntimeError, match="test cleanup failure"):
        await closing
    assert started == set(views)
    assert finished == {"owned-0"}


async def test_scope_preserves_multiple_cleanup_failures(cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    client = create_autospec(Client, instance=True)
    views = _create_targets(client, ("owned-0", "owned-1"))

    monkeypatch.setattr(sandbox, "wait_until_running", _ready_for_targets(views))
    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def delete(_cluster: Cluster, _client: Client, name: str, _uid: object) -> None:
        started.add(name)
        if started == set(views):
            all_started.set()
        await release.wait()
        failure = RuntimeError(f"{name} cleanup failure")
        failure.add_note(f"{name} cleanup diagnostic")
        raise failure

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.get_tracer(__name__)) as create:
            await create("owned-0")
            await create("owned-1")

    closing = asyncio.create_task(use_scope())
    try:
        await asyncio.wait_for(all_started.wait(), timeout=5)
    except TimeoutError:
        release.set()
        await closing
        raise
    assert not closing.done()
    release.set()
    with pytest.raises(ExceptionGroup, match="Sandbox cleanup failed") as raised:
        await closing
    assert started == set(views)
    assert len(raised.value.exceptions) == 2
    assert {str(error) for error in raised.value.exceptions} == {"owned-0 cleanup failure", "owned-1 cleanup failure"}
    assert {note for error in raised.value.exceptions for note in error.__notes__} == {
        "owned-0 cleanup diagnostic",
        "owned-1 cleanup diagnostic",
    }


async def test_scope_cleans_failed_boot_and_preserves_its_error(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    created = _create_targets(client, ("failed-boot",))["failed-boot"]
    cleanup: list[tuple[str, object]] = []

    async def not_ready(
        _client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == created
        raise RuntimeError("boot did not become ready")

    async def delete(_cluster: Cluster, _client: Client, name: str, uid: object) -> None:
        cleanup.append((name, uid))

    monkeypatch.setattr(sandbox, "wait_until_running", not_ready)
    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.get_tracer(__name__)) as create:
            await create("failed-boot")

    with pytest.raises(RuntimeError, match="boot did not become ready"):
        await use_scope()
    assert cleanup == [(created.name, created.uid)]


async def test_scope_captures_failed_vm_before_suspend_and_preserves_readiness_error(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    created = _create_targets(client, ("failed-vm",))["failed-vm"]
    view = _environment(created.name, created.uid, ready=False, pod_uid=str(uuid4()))
    readiness_error = SandboxNotReadyError("VM did not reach Ready", last_view=view)
    events: list[str] = []

    async def not_ready(
        _client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == created
        raise readiness_error

    def capture(failed_view: environment_service_pb2.EnvironmentDescriptor, artifact_root: object) -> object:
        assert failed_view is view
        events.append("capture")
        raise RuntimeError("/proc snapshot unavailable")

    async def delete(_cluster: Cluster, _client: Client, name: str, uid: str) -> None:
        assert (name, uid) == (created.name, created.uid)
        events.append("suspend-delete")

    monkeypatch.setattr(sandbox, "wait_until_running", not_ready)
    monkeypatch.setattr(sandbox.boot_rcu, "capture_readiness_failure", capture)
    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.NoOpTracer()) as create:
            await create(created.name, template=sandbox.VM_TEMPLATE)

    with pytest.raises(SandboxNotReadyError, match="did not reach Ready") as raised:
        await use_scope()

    assert raised.value is readiness_error
    assert events == ["capture", "suspend-delete"]
    assert any("Passive host QEMU readiness snapshot failed" in note for note in raised.value.__notes__)


async def test_scope_skips_capture_when_readiness_view_has_a_replacement_uid(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    created = _create_targets(client, ("failed-vm",))["failed-vm"]
    replacement = _environment(created.name, str(uuid4()), ready=False, pod_uid=str(uuid4()))
    readiness_error = SandboxNotReadyError("VM was replaced", last_view=replacement)
    captures: list[str] = []

    async def not_ready(
        _client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == created
        raise readiness_error

    def capture(view: environment_service_pb2.EnvironmentDescriptor, _artifact_root: object) -> None:
        captures.append(view.target.uid)

    async def delete(_cluster: Cluster, _client: Client, _name: str, _uid: str) -> None:
        return

    monkeypatch.setattr(sandbox, "wait_until_running", not_ready)
    monkeypatch.setattr(sandbox.boot_rcu, "capture_readiness_failure", capture)
    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.NoOpTracer()) as create:
            await create("failed-vm", template=sandbox.VM_TEMPLATE)

    with pytest.raises(SandboxNotReadyError, match="VM was replaced") as raised:
        await use_scope()

    assert raised.value is readiness_error
    assert replacement.target.uid != created.uid
    assert captures == []


async def test_scope_skips_capture_for_pod_failure_and_ready_vm(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    views = _create_targets(client, ("failed-pod", "ready-vm"))
    pod_error = SandboxNotReadyError(
        "Pod did not reach Ready",
        last_view=_environment("failed-pod", views["failed-pod"].uid, ready=False, pod_uid=str(uuid4())),
    )
    ready_vm = _environment("ready-vm", views["ready-vm"].uid, ready=True, pod_uid=str(uuid4()))
    captures: list[str] = []

    async def readiness(
        _client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        if target.name == "failed-pod":
            raise pod_error
        return ready_vm

    def capture(view: environment_service_pb2.EnvironmentDescriptor, _artifact_root: object) -> None:
        captures.append(view.target.name)

    async def delete(_cluster: Cluster, _client: Client, _name: str, _uid: str) -> None:
        return

    async def admitted_launcher(*_args: object) -> None:
        return

    monkeypatch.setattr(sandbox, "wait_until_running", readiness)
    monkeypatch.setattr(sandbox.boot_rcu, "capture_readiness_failure", capture)
    monkeypatch.setattr(sandbox, "assert_admitted_launcher_topology", admitted_launcher)
    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async with sandbox.sandbox_scope(cluster, client, trace.NoOpTracer()) as create:
        with pytest.raises(SandboxNotReadyError, match="Pod did not reach Ready"):
            await create("failed-pod")
        assert await create("ready-vm", template=sandbox.VM_TEMPLATE) is ready_vm

    assert captures == []


async def test_scope_body_cancellation_waits_for_cleanup_and_keeps_failures(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    views = _create_targets(client, ("owned-0", "owned-1"))

    monkeypatch.setattr(sandbox, "wait_until_running", _ready_for_targets(views))
    body_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()
    finished: set[str] = set()

    async def delete(_cluster: Cluster, _client: Client, name: str, _uid: object) -> None:
        started.add(name)
        if started == set(views):
            cleanup_started.set()
        await release.wait()
        if name == "owned-1":
            error = RuntimeError("test cleanup failure")
            error.add_note("test cleanup diagnostic")
            raise error
        finished.add(name)

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.get_tracer(__name__)) as create:
            await create("owned-0")
            await create("owned-1")
            body_started.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(use_scope())
    await body_started.wait()
    running.cancel("body cancellation")
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
    except TimeoutError:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        raise
    assert not running.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await running
    assert str(raised.value) == "body cancellation"
    assert started == set(views)
    assert finished == {"owned-0"}
    cleanup_error = raised.value.__cause__
    assert isinstance(cleanup_error, RuntimeError)
    assert str(cleanup_error) == "test cleanup failure"
    assert cleanup_error.__notes__ == ["test cleanup diagnostic"]


async def test_scope_cancellation_during_cleanup_waits_for_every_owner(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    views = _create_targets(client, ("owned-0", "owned-1"))

    monkeypatch.setattr(sandbox, "wait_until_running", _ready_for_targets(views))
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()
    finished: set[str] = set()

    async def delete(_cluster: Cluster, _client: Client, name: str, _uid: object) -> None:
        started.add(name)
        if started == set(views):
            cleanup_started.set()
        await release.wait()
        finished.add(name)

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def use_scope() -> None:
        async with sandbox.sandbox_scope(cluster, client, trace.get_tracer(__name__)) as create:
            await create("owned-0")
            await create("owned-1")

    closing = asyncio.create_task(use_scope())
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
    except TimeoutError:
        release.set()
        await closing
        raise
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert started == set(views)
    assert finished == set(views)


if __name__ == "__main__":
    pytest_bazel.main()
