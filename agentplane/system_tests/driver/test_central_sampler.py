"""Ownership behavior for the native Kubernetes Sandbox adapter."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, create_autospec

import pytest
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException

# gazelle:include_dep @pypi//protobuf
from agentplane.proto.identity_pb2 import EnvironmentTarget
from agentplane.system_tests.driver import central_sampler, sandbox_cleanup
from agentplane.system_tests.driver.cluster import Cluster


def _cluster() -> Cluster:
    cluster = create_autospec(Cluster, instance=True)
    cluster.namespace = "test"
    return cast(Cluster, cluster)


async def test_unknown_create_outcome_is_not_replayed_or_assumed_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _cluster()
    kubectl = cast(AsyncMock, cluster.kubectl)
    kubectl.side_effect = TimeoutError("create response was lost")
    identity = {"namespace": "test", "name": "owned-identity", "uid": "identity-uid"}
    create_identity = AsyncMock(return_value=identity)
    delete_identity = AsyncMock()
    monkeypatch.setattr(central_sampler, "create_scenario_identity", create_identity)
    monkeypatch.setattr(central_sampler, "delete_scenario_identity", delete_identity)

    with pytest.raises(TimeoutError, match="response was lost"):
        async with central_sampler._owned_native_sandbox(cluster, "owned-sandbox", "pod"):
            pytest.fail("an unknown create must not enter the Sandbox scope")

    create_identity.assert_awaited_once()
    kubectl.assert_awaited_once()
    delete_identity.assert_not_awaited()


async def test_acknowledged_native_sandbox_waits_for_release_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster()
    created = {"metadata": {"namespace": "test", "name": "owned-sandbox", "uid": "owned-uid"}}
    cast(AsyncMock, cluster.kubectl).return_value = json.dumps(created)
    identity = {"namespace": "test", "name": "owned-identity", "uid": "identity-uid"}
    delete_identity = AsyncMock()
    monkeypatch.setattr(central_sampler, "create_scenario_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(central_sampler, "delete_scenario_identity", delete_identity)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    released: list[EnvironmentTarget] = []
    events: list[str] = []

    async def release(_cluster: Cluster, name: str, uid: str) -> None:
        released.append(EnvironmentTarget(namespace="test", name=name, uid=uid))
        events.append("sandbox-release-started")
        release_started.set()
        await allow_release.wait()
        events.append("sandbox-released")

    async def delete_identity_after_release(_cluster: Cluster, _identity: object) -> None:
        events.append("identity-deleted")

    delete_identity.side_effect = delete_identity_after_release
    monkeypatch.setattr(sandbox_cleanup, "delete_sandbox", release)
    entered = asyncio.Event()

    async def use_sandbox() -> None:
        async with central_sampler._owned_native_sandbox(cluster, "owned-sandbox", "pod"):
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(use_sandbox())
    await asyncio.wait_for(entered.wait(), timeout=5)
    running.cancel("scenario cancelled")
    await asyncio.wait_for(release_started.wait(), timeout=5)
    assert not running.done()
    delete_identity.assert_not_awaited()
    allow_release.set()

    with pytest.raises(asyncio.CancelledError, match="scenario cancelled"):
        await running

    assert released == [EnvironmentTarget(namespace="test", name="owned-sandbox", uid="owned-uid")]
    assert events == ["sandbox-release-started", "sandbox-released", "identity-deleted"]
    delete_identity.assert_awaited_once_with(cluster, identity)


async def test_native_cleanup_keeps_scenario_failure_and_records_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _cluster()
    created = {"metadata": {"namespace": "test", "name": "owned-sandbox", "uid": "owned-uid"}}
    cast(AsyncMock, cluster.kubectl).return_value = json.dumps(created)
    identity = {"namespace": "test", "name": "owned-identity", "uid": "identity-uid"}
    events: list[str] = []
    scenario_error = AssertionError("scenario failed")

    async def delete_identity(_cluster: Cluster, _identity: object) -> None:
        events.append("identity-delete")

    async def fail_release(_cluster: Cluster, name: str, uid: str) -> None:
        assert (name, uid) == ("owned-sandbox", "owned-uid")
        events.append("sandbox-release")
        raise RuntimeError("native cleanup failed")

    monkeypatch.setattr(central_sampler, "create_scenario_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(central_sampler, "delete_scenario_identity", delete_identity)
    monkeypatch.setattr(sandbox_cleanup, "delete_sandbox", fail_release)

    with pytest.raises(AssertionError, match="scenario failed") as raised:
        async with central_sampler._owned_native_sandbox(cluster, "owned-sandbox", "pod"):
            raise scenario_error

    assert raised.value is scenario_error
    assert raised.value.__notes__ == ["Sandbox cleanup also failed (RuntimeError)."]
    assert events == ["sandbox-release", "identity-delete"]


async def test_sandbox_delete_preconditions_the_acknowledged_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _cluster()
    kubectl = cast(AsyncMock, cluster.kubectl)
    kubectl.return_value = ""
    current_uid = "replacement-uid"
    delete_options: list[k8s.V1DeleteOptions] = []

    class FakeCustomObjectsApi:
        def __init__(self, _connection: object) -> None:
            pass

        async def delete_namespaced_custom_object(self, *_args: object, body: object, **_kwargs: object) -> None:
            body = cast(k8s.V1DeleteOptions, body)
            delete_options.append(body)
            assert body.preconditions.uid == "owned-uid"
            assert body.preconditions.uid != current_uid
            raise ApiException(status=409, reason="UID precondition did not match replacement")

    @asynccontextmanager
    async def api_client():
        yield object()

    monkeypatch.setattr(cluster, "api_client", api_client)
    monkeypatch.setattr(central_sampler.k8s, "CustomObjectsApi", FakeCustomObjectsApi)

    with pytest.raises(ApiException) as raised:
        await sandbox_cleanup.delete_sandbox(cluster, "owned-sandbox", "owned-uid")

    assert raised.value.status == 409
    assert len(delete_options) == 1
    assert kubectl.await_count == 3
