"""Scenario ownership overlaps teardown and drains resources on failure or cancellation."""

import asyncio
from datetime import UTC, datetime
from types import TracebackType
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.app.testing.client import Client
from agentplane.app.testing.models import NewSandbox, SandboxPhase, SandboxView
from agentplane.subjects import ServiceAccountRef
from agentplane.system_tests import native_artifacts, sandbox, scenario
from agentplane.system_tests.cluster import Cluster


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    views: dict[str, SandboxView] = {}

    def new_client(*, base_url: str, token: str) -> Client:
        client = create_autospec(Client, instance=True)
        client.__aenter__.return_value = client
        client.opened_sandbox_uids = set()

        async def create(spec: NewSandbox) -> SandboxView:
            view = views[spec.name] = SandboxView(
                name=spec.name,
                uid=uuid4(),
                namespace="test",
                created_at=datetime.now(UTC),
                service_account=ServiceAccountRef(namespace="test", name="runner"),
                phase=SandboxPhase.CREATING,
            )
            client.opened_sandbox_uids.add(view.uid)
            return view

        async def close(
            opened: Client, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
        ) -> None:
            events.extend(
                ("client-close", view.name) for view in views.values() if view.uid in opened.opened_sandbox_uids
            )

        client.create_sandbox.side_effect = create
        client.__aexit__.side_effect = close
        return cast(Client, client)

    async def ready(client: Client, name: str, *, expected_uid: UUID) -> SandboxView:
        assert expected_uid == views[name].uid
        return views[name]

    async def capture(client: Client, *, sandbox_uid: UUID) -> None:
        (view,) = [view for view in views.values() if view.uid == sandbox_uid]
        events.append(("diagnostics", view.name))

    async def delete(cluster: Cluster, client: Client, name: str, uid: UUID) -> None:
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

        async def ready(client: Client, name: str, *, expected_uid: UUID) -> SandboxView:
            if name == "second":
                raise RuntimeError("readiness")
            return await original_ready(client, name, expected_uid=expected_uid)

        monkeypatch.setattr(sandbox, "wait_until_running", ready)
    elif failure == "diagnostics":

        async def capture(client: Client, *, sandbox_uid: UUID) -> None:
            raise RuntimeError("diagnostics")

        monkeypatch.setattr(native_artifacts, "capture", capture)
    elif failure == "deletion":
        original_delete = sandbox.suspend_and_delete

        async def delete(cluster: Cluster, client: Client, name: str, uid: UUID) -> None:
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

    async def delete(cluster: Cluster, client: Client, name: str, uid: UUID) -> None:
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


if __name__ == "__main__":
    pytest_bazel.main()
