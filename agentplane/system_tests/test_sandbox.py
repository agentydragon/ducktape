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


if __name__ == "__main__":
    pytest_bazel.main()
