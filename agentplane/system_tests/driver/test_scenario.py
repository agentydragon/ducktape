"""Scenario ownership overlaps teardown and drains resources on failure or cancellation."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, cast
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import app_sandbox, native_artifacts, scenario
from agentplane.system_tests.driver.cluster import Cluster


@dataclass(frozen=True)
class LifecycleEvent:
    operation: Literal["diagnostics", "delete", "client-close"]
    sandbox_name: str


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[LifecycleEvent]:
    events: list[LifecycleEvent] = []
    views: dict[str, environment_service_pb2.EnvironmentDescriptor] = {}

    @asynccontextmanager
    async def new_dispatcher(_cluster: Cluster) -> AsyncIterator[SessionDispatcherClient]:
        yield cast(SessionDispatcherClient, create_autospec(SessionDispatcherClient, instance=True))

    @asynccontextmanager
    async def new_client(*, base_url: str, token: str, dispatcher: SessionDispatcherClient) -> AsyncIterator[Client]:
        del base_url, token
        assert isinstance(dispatcher, SessionDispatcherClient)
        client = create_autospec(Client, instance=True)
        client.opened_sandbox_uids = set()

        async def create(spec: environment_service_pb2.CreateEnvironmentRequest) -> identity_pb2.EnvironmentTarget:
            assert spec.identity == spec.name
            view = views[spec.name] = environment_service_pb2.EnvironmentDescriptor(
                target=identity_pb2.EnvironmentTarget(namespace="test", name=spec.name, uid=str(uuid4())),
                execution_account=identity_pb2.ServiceAccountIdentity(
                    account=identity_pb2.ServiceAccount(namespace="test", name="runner")
                ),
            )
            view.kubernetes_diagnostics.sandbox_status.update({"runnerCredential": {"kind": "Pod"}})
            client.opened_sandbox_uids.add(view.target.uid)
            return view.target

        async def close(opened: Client) -> None:
            events.extend(
                LifecycleEvent("client-close", view.target.name)
                for view in views.values()
                if view.target.uid in opened.opened_sandbox_uids
            )

        client.create_sandbox.side_effect = create
        try:
            yield cast(Client, client)
        finally:
            await close(cast(Client, client))

    async def create_identity(_cluster: Cluster, name: str, _policy_sets: list[str]) -> dict[str, str]:
        return {"namespace": "test", "name": name, "uid": f"identity-{name}"}

    async def delete_identity(_cluster: Cluster, _identity: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(app_sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(app_sandbox, "delete_scenario_identity", delete_identity)

    async def ready(
        client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == views[target.name].target
        return views[target.name]

    async def capture(client: Client, *, sandbox_uid: str) -> None:
        (view,) = [view for view in views.values() if view.target.uid == sandbox_uid]
        events.append(LifecycleEvent("diagnostics", view.target.name))

    async def delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
        events.append(LifecycleEvent("delete", name))

    monkeypatch.setattr(Client, "connect", staticmethod(new_client))
    monkeypatch.setattr(scenario, "native_client", new_dispatcher)
    monkeypatch.setattr(app_sandbox, "wait_until_running", ready)
    monkeypatch.setattr(app_sandbox, "suspend_and_delete", delete)
    monkeypatch.setattr(native_artifacts, "capture", capture)
    return events


@pytest.fixture
def cluster() -> Cluster:
    return cast(Cluster, create_autospec(Cluster, instance=True))


async def test_finished_scenario_releases_its_guest_while_peer_is_running(
    cluster: Cluster, lifecycle: list[LifecycleEvent]
) -> None:
    peer_started = asyncio.Event()
    finished = asyncio.Event()
    clients: list[Client] = []

    async def fast() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (client, create):
            clients.append(client)
            await create("fast")
            await peer_started.wait()
        finished.set()

    async def slow() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (client, create):
            clients.append(client)
            await create("slow")
            peer_started.set()
            await finished.wait()
            assert LifecycleEvent("delete", "fast") in lifecycle
            assert LifecycleEvent("client-close", "fast") in lifecycle
            assert LifecycleEvent("delete", "slow") not in lifecycle

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(fast())
        tasks.create_task(slow())

    assert clients[0] is not clients[1]
    for name in ("fast", "slow"):
        assert lifecycle.index(LifecycleEvent("diagnostics", name)) < lifecycle.index(LifecycleEvent("delete", name))
        assert lifecycle.index(LifecycleEvent("delete", name)) < lifecycle.index(LifecycleEvent("client-close", name))


@pytest.mark.parametrize("failure", ["readiness", "assertions", "diagnostics", "deletion"])
async def test_failure_still_releases_every_created_guest_and_closes_client(
    cluster: Cluster, lifecycle: list[LifecycleEvent], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "readiness":
        original_ready = app_sandbox.wait_until_running

        async def ready(
            client: Client, target: identity_pb2.EnvironmentTarget
        ) -> environment_service_pb2.EnvironmentDescriptor:
            if target.name == "second":
                raise RuntimeError("readiness")
            return await original_ready(client, target)

        monkeypatch.setattr(app_sandbox, "wait_until_running", ready)
    elif failure == "diagnostics":

        async def capture(client: Client, *, sandbox_uid: str) -> None:
            raise RuntimeError("diagnostics")

        monkeypatch.setattr(native_artifacts, "capture", capture)
    elif failure == "deletion":
        original_delete = app_sandbox.suspend_and_delete

        async def delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
            await original_delete(cluster, client, name, uid)
            if name == "second":
                raise RuntimeError("deletion")

        monkeypatch.setattr(app_sandbox, "suspend_and_delete", delete)

    async def run() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (_, create):
            await create("first")
            await create("second")
            if failure == "assertions":
                raise RuntimeError("assertions")

    with pytest.raises(RuntimeError, match=failure):
        await run()

    for name in ("first", "second"):
        assert LifecycleEvent("delete", name) in lifecycle
        assert lifecycle.index(LifecycleEvent("delete", name)) < lifecycle.index(LifecycleEvent("client-close", name))


@pytest.mark.parametrize("failure", ["diagnostics", "deletion"])
async def test_scenario_failure_remains_primary_when_cleanup_also_fails(
    cluster: Cluster, lifecycle: list[LifecycleEvent], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def cleanup_failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cleanup failed")

    if failure == "diagnostics":
        monkeypatch.setattr(native_artifacts, "capture", cleanup_failure)
    else:
        monkeypatch.setattr(app_sandbox, "suspend_and_delete", cleanup_failure)
    original = AssertionError("scenario failed")

    async def run() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (_, create):
            await create("owned")
            raise original

    with pytest.raises(AssertionError) as caught:
        await run()

    assert caught.value is original
    assert LifecycleEvent("client-close", "owned") in lifecycle


async def test_cancellation_during_teardown_waits_for_deletion_before_closing_client(
    cluster: Cluster, lifecycle: list[LifecycleEvent], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleting = asyncio.Event()
    release = asyncio.Event()
    original_delete = app_sandbox.suspend_and_delete

    async def delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
        deleting.set()
        await release.wait()
        await original_delete(cluster, client, name, uid)

    monkeypatch.setattr(app_sandbox, "suspend_and_delete", delete)

    async def run() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (_, create):
            await create("owned")

    async with asyncio.timeout(2):
        task = asyncio.create_task(run())
        await deleting.wait()
        for _ in range(3):
            task.cancel()
            delivered = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(delivered.set_result, None)
            await delivered
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert lifecycle.index(LifecycleEvent("delete", "owned")) < lifecycle.index(LifecycleEvent("client-close", "owned"))


async def test_pod_case_can_disable_native_capture_and_still_delete_its_guests(
    cluster: Cluster, lifecycle: list[LifecycleEvent]
) -> None:
    async with scenario.open_resources(
        cluster, "https://app.test", "test", trace.NoOpTracer(), native_diagnostics=False
    ) as (_, create):
        await create("pod")
    assert LifecycleEvent("diagnostics", "pod") not in lifecycle
    assert lifecycle == [LifecycleEvent("delete", "pod"), LifecycleEvent("client-close", "pod")]
