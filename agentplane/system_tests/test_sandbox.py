"""Sandbox lifecycle helpers wait for authoritative state and bound cleanup."""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, call, create_autospec
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.app.testing.models import SandboxesSnapshot, WatchHealth
from agentplane.sandbox_resource import OperatingMode
from agentplane.system_tests import sandbox
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.sandbox import SandboxNotReadyError, suspend_and_delete


@pytest.fixture
def cluster() -> Cluster:
    access = create_autospec(Cluster, instance=True)
    access.kubectl.side_effect = RuntimeError("test diagnostics unavailable")
    return cast(Cluster, access)


async def test_cleanup_waits_for_effective_suspension_and_actual_deletion(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)
    uid = uuid4()
    closed = False
    deletion_observed = False

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        nonlocal closed, deletion_observed
        try:
            for name, healthy, mode in [
                ("owned-test-sandbox", True, OperatingMode.RUNNING),
                ("owned-test-sandbox", False, OperatingMode.SUSPENDED),
                ("other-sandbox", True, OperatingMode.SUSPENDED),
                ("owned-test-sandbox", True, OperatingMode.SUSPENDED),
            ]:
                client.delete_sandbox.assert_not_awaited()
                yield Mock(
                    watch=Mock(healthy=healthy), sandboxes=[SimpleNamespace(name=name, uid=uid, operating_mode=mode)]
                )
            client.delete_sandbox.assert_awaited_once_with("owned-test-sandbox")
            yield SandboxesSnapshot(watch=WatchHealth(healthy=False, failing={}), sandboxes=[])
            deletion_observed = True
            yield SandboxesSnapshot(watch=WatchHealth(healthy=True, failing={}), sandboxes=[])
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()

    await suspend_and_delete(cluster, client, "owned-test-sandbox", uid)

    assert client.mock_calls == [
        call.suspend_sandbox("owned-test-sandbox"),
        call.sandbox_snapshots(),
        call.delete_sandbox("owned-test-sandbox"),
    ]
    assert closed
    assert deletion_observed


async def test_cleanup_does_not_delete_a_replacement_sandbox(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        yield Mock(
            watch=Mock(healthy=True),
            sandboxes=[SimpleNamespace(name="owned-test-sandbox", uid=uuid4(), operating_mode=OperatingMode.SUSPENDED)],
        )

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(SandboxNotReadyError, match="has been replaced"):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", uuid4())
    client.delete_sandbox.assert_not_awaited()


async def test_cleanup_bounds_a_watch_that_stops_making_progress(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "SANDBOX_READY_SECONDS", 0.01)
    client = create_autospec(Client, instance=True)
    closed = False

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        nonlocal closed
        try:
            yield Mock(watch=Mock(healthy=False), sandboxes=[])
            await asyncio.Event().wait()
        finally:
            closed = True

    client.sandbox_snapshots.return_value = snapshots()
    with pytest.raises(TimeoutError):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", uuid4())
    client.delete_sandbox.assert_not_awaited()
    assert closed


async def test_cleanup_does_not_retry_an_authorization_refusal_or_delete(cluster: Cluster) -> None:
    client = create_autospec(Client, instance=True)
    request = httpx.Request("GET", "https://app.test.invalid/sandboxes/owned-test-sandbox")
    refused = httpx.HTTPStatusError("read refused", request=request, response=httpx.Response(403, request=request))

    async def snapshots() -> AsyncIterator[SandboxesSnapshot]:
        raise refused
        yield  # pragma: no cover

    client.sandbox_snapshots.return_value = snapshots()

    with pytest.raises(httpx.HTTPStatusError):
        await suspend_and_delete(cluster, client, "owned-test-sandbox", uuid4())

    client.sandbox_snapshots.assert_called_once()
    client.delete_sandbox.assert_not_awaited()


async def test_scope_releases_owned_sandboxes_concurrently(cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    client = create_autospec(Client, instance=True)
    views = {name: SimpleNamespace(name=name, uid=uuid4(), pod=None, status={}) for name in ("owned-0", "owned-1")}
    client.create_sandbox.side_effect = list(views.values())

    async def ready(_client: Client, name: str) -> SimpleNamespace:
        return views[name]

    monkeypatch.setattr(sandbox, "wait_until_running", ready)
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
    views = {name: SimpleNamespace(name=name, uid=uuid4(), pod=None, status={}) for name in ("owned-0", "owned-1")}
    client.create_sandbox.side_effect = list(views.values())

    async def ready(_client: Client, name: str) -> SimpleNamespace:
        return views[name]

    monkeypatch.setattr(sandbox, "wait_until_running", ready)
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
    views = {name: SimpleNamespace(name=name, uid=uuid4(), pod=None, status={}) for name in ("owned-0", "owned-1")}
    client.create_sandbox.side_effect = list(views.values())

    async def ready(_client: Client, name: str) -> SimpleNamespace:
        return views[name]

    monkeypatch.setattr(sandbox, "wait_until_running", ready)
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
    view = SimpleNamespace(name="failed-boot", uid=uuid4(), pod=None, status={})
    client.create_sandbox.return_value = view
    cleanup: list[tuple[str, object]] = []

    async def not_ready(_client: Client, _name: str) -> SimpleNamespace:
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
    assert cleanup == [(view.name, view.uid)]


async def test_scope_body_cancellation_waits_for_cleanup_and_keeps_failures(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_autospec(Client, instance=True)
    views = {name: SimpleNamespace(name=name, uid=uuid4(), pod=None, status={}) for name in ("owned-0", "owned-1")}
    client.create_sandbox.side_effect = list(views.values())

    async def ready(_client: Client, name: str) -> SimpleNamespace:
        return views[name]

    monkeypatch.setattr(sandbox, "wait_until_running", ready)
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
    views = {name: SimpleNamespace(name=name, uid=uuid4(), pod=None, status={}) for name in ("owned-0", "owned-1")}
    client.create_sandbox.side_effect = list(views.values())

    async def ready(_client: Client, name: str) -> SimpleNamespace:
        return views[name]

    monkeypatch.setattr(sandbox, "wait_until_running", ready)
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
