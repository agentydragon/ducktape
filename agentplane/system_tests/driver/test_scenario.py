"""Scenario ownership overlaps teardown and drains resources on failure or cancellation."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
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
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    views: dict[str, environment_service_pb2.EnvironmentDescriptor] = {}

    @asynccontextmanager
    async def new_app_client(*, base_url: str, token: str) -> AsyncIterator[Client]:
        del base_url, token
        client = create_autospec(Client, instance=True)

        try:
            yield cast(Client, client)
        finally:
            events.append(("app-close", "scenario"))

    @asynccontextmanager
    async def new_environment_client(_cluster: Cluster, token_file: Path) -> AsyncIterator[EnvironmentServiceClient]:
        events.append(("gateway-token", str(token_file)))
        environments = create_autospec(EnvironmentServiceClient, instance=True)
        created_uids: set[str] = set()

        async def create(spec: environment_service_pb2.CreateEnvironmentRequest) -> identity_pb2.EnvironmentTarget:
            assert spec.identity == spec.name
            view = views[spec.name] = environment_service_pb2.EnvironmentDescriptor(
                target=identity_pb2.EnvironmentTarget(namespace="test", name=spec.name, uid=str(uuid4())),
                execution_account=identity_pb2.ServiceAccountIdentity(
                    account=identity_pb2.ServiceAccount(namespace="test", name="runner")
                ),
            )
            created_uids.add(view.target.uid)
            return view.target

        environments.create_environment.side_effect = create
        try:
            yield cast(EnvironmentServiceClient, environments)
        finally:
            events.extend(
                ("environment-close", view.target.name) for view in views.values() if view.target.uid in created_uids
            )

    async def create_identity(_cluster: Cluster, name: str, _policy_sets: list[str]) -> dict[str, str]:
        return {"namespace": "test", "name": name, "uid": f"identity-{name}"}

    async def delete_identity(_cluster: Cluster, _identity: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(sandbox, "create_scenario_identity", create_identity)
    monkeypatch.setattr(sandbox, "delete_scenario_identity", delete_identity)

    async def ready(
        _environments: EnvironmentServiceClient, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        assert target == views[target.name].target
        return views[target.name]

    async def capture(_client: Client, *, sandbox_uid: str) -> None:
        (view,) = [view for view in views.values() if view.target.uid == sandbox_uid]
        events.append(("diagnostics", view.target.name))

    async def delete(_cluster: Cluster, _environments: EnvironmentServiceClient, name: str, _uid: str) -> None:
        events.append(("delete", name))

    monkeypatch.setattr(Client, "connect", staticmethod(new_app_client))
    monkeypatch.setattr(scenario, "native_environment_client", new_environment_client)
    monkeypatch.setattr(sandbox, "wait_until_running", ready)
    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)
    monkeypatch.setattr(native_artifacts, "capture", capture)
    return events


@pytest.fixture
def cluster() -> Cluster:
    return cast(Cluster, create_autospec(Cluster, instance=True))


def _open(cluster: Cluster, *, native_diagnostics: bool = True):
    return scenario.open_resources(
        cluster,
        "https://app.test",
        "test",
        trace.NoOpTracer(),
        token_file=Path("gateway-token"),
        native_diagnostics=native_diagnostics,
    )


async def test_finished_scenario_releases_its_guest_while_peer_is_running(
    cluster: Cluster, lifecycle: list[tuple[str, str]]
) -> None:
    peer_started = asyncio.Event()
    finished = asyncio.Event()
    clients: list[Client] = []

    async def fast() -> None:
        async with _open(cluster) as (client, create):
            clients.append(client)
            await create("fast")
            await peer_started.wait()
        finished.set()

    async def slow() -> None:
        async with _open(cluster) as (client, create):
            clients.append(client)
            await create("slow")
            peer_started.set()
            await finished.wait()
            assert ("delete", "fast") in lifecycle
            assert ("environment-close", "fast") in lifecycle
            assert ("delete", "slow") not in lifecycle

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(fast())
        tasks.create_task(slow())

    assert clients[0] is not clients[1]
    token_files = [value for event, value in lifecycle if event == "gateway-token"]
    assert len(token_files) == len(set(token_files)) == 2
    for name in ("fast", "slow"):
        assert lifecycle.index(("diagnostics", name)) < lifecycle.index(("delete", name))
        assert lifecycle.index(("delete", name)) < lifecycle.index(("environment-close", name))
    assert lifecycle.index(("environment-close", "fast")) < lifecycle.index(("app-close", "scenario"))


@pytest.mark.parametrize("failure", ["readiness", "assertions", "diagnostics", "deletion"])
async def test_failure_still_releases_every_created_guest_and_closes_client(
    cluster: Cluster, lifecycle: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "readiness":
        original_ready = sandbox.wait_until_running

        async def ready(
            environments: EnvironmentServiceClient, target: identity_pb2.EnvironmentTarget
        ) -> environment_service_pb2.EnvironmentDescriptor:
            if target.name == "second":
                raise RuntimeError("readiness")
            return await original_ready(environments, target)

        monkeypatch.setattr(sandbox, "wait_until_running", ready)
    elif failure == "diagnostics":

        async def capture(_client: Client, *, sandbox_uid: str) -> None:
            raise RuntimeError("diagnostics")

        monkeypatch.setattr(native_artifacts, "capture", capture)
    elif failure == "deletion":
        original_delete = sandbox.suspend_and_delete

        async def delete(cluster: Cluster, environments: EnvironmentServiceClient, name: str, uid: str) -> None:
            await original_delete(cluster, environments, name, uid)
            if name == "second":
                raise RuntimeError("deletion")

        monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def run() -> None:
        async with _open(cluster) as (_, create):
            await create("first")
            await create("second")
            if failure == "assertions":
                raise RuntimeError("assertions")

    with pytest.raises(RuntimeError, match=failure):
        await run()

    for name in ("first", "second"):
        assert ("delete", name) in lifecycle
        assert lifecycle.index(("delete", name)) < lifecycle.index(("environment-close", name))


async def test_cancellation_during_teardown_waits_for_deletion_before_closing_client(
    cluster: Cluster, lifecycle: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleting = asyncio.Event()
    release = asyncio.Event()
    original_delete = sandbox.suspend_and_delete

    async def delete(cluster: Cluster, environments: EnvironmentServiceClient, name: str, uid: str) -> None:
        deleting.set()
        await release.wait()
        await original_delete(cluster, environments, name, uid)

    monkeypatch.setattr(sandbox, "suspend_and_delete", delete)

    async def run() -> None:
        async with _open(cluster) as (_, create):
            await create("owned")

    async with asyncio.timeout(2):
        task = asyncio.create_task(run())
        await deleting.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert lifecycle.index(("delete", "owned")) < lifecycle.index(("environment-close", "owned"))


async def test_pod_case_can_disable_native_capture_and_still_delete_its_guests(
    cluster: Cluster, lifecycle: list[tuple[str, str]]
) -> None:
    async with _open(cluster, native_diagnostics=False) as (_, create):
        await create("pod")
    assert ("diagnostics", "pod") not in lifecycle
    assert [event for event in lifecycle if event[0] != "gateway-token"] == [
        ("delete", "pod"),
        ("environment-close", "pod"),
        ("app-close", "scenario"),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
