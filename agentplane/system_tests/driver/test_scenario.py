"""Scenario ownership overlaps teardown and drains resources on failure or cancellation."""

import asyncio
from types import TracebackType
from typing import cast
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver import native_artifacts, sandbox, scenario
from agentplane.system_tests.driver.cluster import Cluster


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    views: dict[str, environment_service_pb2.EnvironmentDescriptor] = {}

    def new_client(*, base_url: str, token: str) -> Client:
        client = create_autospec(Client, instance=True)
        client.__aenter__.return_value = client
        client.opened_sandbox_uids = set()

        async def create(spec: environment_service_pb2.CreateEnvironmentRequest) -> identity_pb2.EnvironmentTarget:
            assert spec.identity == spec.name
            view = views[spec.name] = environment_service_pb2.EnvironmentDescriptor(
                target=identity_pb2.EnvironmentTarget(namespace="test", name=spec.name, uid=str(uuid4())),
                execution_account=identity_pb2.ServiceAccountIdentity(
                    account=identity_pb2.ServiceAccount(namespace="test", name="runner")
                ),
            )
            client.opened_sandbox_uids.add(view.target.uid)
            return view.target

        async def close(
            opened: Client, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
        ) -> None:
            events.extend(
                ("client-close", view.target.name)
                for view in views.values()
                if view.target.uid in opened.opened_sandbox_uids
            )

        client.create_sandbox.side_effect = create
        client.__aexit__.side_effect = close
        return cast(Client, client)

    async def create_identity(_cluster: Cluster, name: str, _policy_sets: list[str]) -> dict[str, str]:
        return {"namespace": "test", "name": name, "uid": f"identity-{name}"}

    async def delete_identity(_cluster: Cluster, _identity: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(sandbox, "delete_scenario_identity", delete_identity)

    async def ready(
        client: Client, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == views[target.name].target
        return views[target.name]

    async def capture(client: Client, *, sandbox_uid: str) -> None:
        (view,) = [view for view in views.values() if view.target.uid == sandbox_uid]
        events.append(("diagnostics", view.target.name))

    async def delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
        events.append(("delete", name))

    monkeypatch.setattr(scenario, "Client", new_client)
    monkeypatch.setattr(sandbox, "wait_until_running", ready)
    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)
    monkeypatch.setattr(native_artifacts, "capture", capture)
    return events


@pytest.fixture
def cluster() -> Cluster:
    return cast(Cluster, create_autospec(Cluster, instance=True))


async def test_finished_scenario_releases_its_guest_while_peer_is_running(
    cluster: Cluster, lifecycle: list[tuple[str, str]]
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
            assert ("delete", "fast") in lifecycle
            assert ("client-close", "fast") in lifecycle
            assert ("delete", "slow") not in lifecycle

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(fast())
        tasks.create_task(slow())

    assert clients[0] is not clients[1]
    for name in ("fast", "slow"):
        assert lifecycle.index(("diagnostics", name)) < lifecycle.index(("delete", name))
        assert lifecycle.index(("delete", name)) < lifecycle.index(("client-close", name))


@pytest.mark.parametrize("failure", ["readiness", "assertions", "diagnostics", "deletion"])
async def test_failure_still_releases_every_created_guest_and_closes_client(
    cluster: Cluster, lifecycle: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "readiness":
        original_ready = sandbox.wait_until_running

        async def ready(
            client: Client, target: identity_pb2.EnvironmentTarget
        ) -> environment_service_pb2.EnvironmentDescriptor:
            if target.name == "second":
                raise RuntimeError("readiness")
            return await original_ready(client, target)

        monkeypatch.setattr(sandbox, "wait_until_running", ready)
    elif failure == "diagnostics":

        async def capture(client: Client, *, sandbox_uid: str) -> None:
            raise RuntimeError("diagnostics")

        monkeypatch.setattr(native_artifacts, "capture", capture)
    elif failure == "deletion":
        original_delete = sandbox.suspend_and_delete

        async def delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
            await original_delete(cluster, client, name, uid)
            if name == "second":
                raise RuntimeError("deletion")

        monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def run() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (_, create):
            await create("first")
            await create("second")
            if failure == "assertions":
                raise RuntimeError("assertions")

    with pytest.raises(RuntimeError, match=failure):
        await run()

    for name in ("first", "second"):
        assert ("delete", name) in lifecycle
        assert lifecycle.index(("delete", name)) < lifecycle.index(("client-close", name))


async def test_cancellation_during_teardown_waits_for_deletion_before_closing_client(
    cluster: Cluster, lifecycle: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleting = asyncio.Event()
    release = asyncio.Event()
    original_delete = sandbox.suspend_and_delete

    async def delete(cluster: Cluster, client: Client, name: str, uid: str) -> None:
        deleting.set()
        await release.wait()
        await original_delete(cluster, client, name, uid)

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def run() -> None:
        async with scenario.open_resources(cluster, "https://app.test", "test", trace.NoOpTracer()) as (_, create):
            await create("owned")

    async with asyncio.timeout(2):
        task = asyncio.create_task(run())
        await deleting.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert lifecycle.index(("delete", "owned")) < lifecycle.index(("client-close", "owned"))


async def test_pod_case_can_disable_native_capture_and_still_delete_its_guests(
    cluster: Cluster, lifecycle: list[tuple[str, str]]
) -> None:
    async with scenario.open_resources(
        cluster, "https://app.test", "test", trace.NoOpTracer(), native_diagnostics=False
    ) as (_, create):
        await create("pod")
    assert ("diagnostics", "pod") not in lifecycle
    assert lifecycle == [("delete", "pod"), ("client-close", "pod")]


if __name__ == "__main__":
    pytest_bazel.main()
