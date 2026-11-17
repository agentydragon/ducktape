"""Worker-local reuse and the boundary after a failed guest deletion."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast
from unittest.mock import create_autospec

import pytest

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import scenario
from agentplane.system_tests.driver.case_batch import Case, CaseBatch
from agentplane.system_tests.driver.cleanup import CleanupError
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.suites.pod_worker import Worker

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize("delete_fails", [False, True], ids=["reuse", "failed-deletion"])
async def test_worker_reuse_and_fresh_case_ownership(monkeypatch: pytest.MonkeyPatch, delete_fails: bool) -> None:
    created: list[environment_service_pb2.EnvironmentDescriptor] = []
    deleted: list[str] = []
    client = create_autospec(Client, instance=True)
    client.get_environment.side_effect = lambda target: next(view for view in created if view.target == target)

    @asynccontextmanager
    async def resources(*args: object, **kwargs: object) -> AsyncIterator[tuple[Client, Sandboxes]]:
        owned: list[str] = []

        async def create(name: str) -> environment_service_pb2.EnvironmentDescriptor:
            view = environment_service_pb2.EnvironmentDescriptor()
            view.target.name = name
            view.target.uid = f"uid-{len(created)}"
            view.kubernetes_diagnostics.pod.uid = f"pod-{len(created)}"
            created.append(view)
            owned.append(view.target.uid)
            return view

        try:
            yield client, create
        finally:
            deleted.extend(owned)
            if owned and delete_fails:
                raise RuntimeError("guest deletion failed")

    monkeypatch.setattr(scenario, "open_resources", resources)

    def worker_scope() -> AbstractAsyncContextManager[Worker]:
        return Worker.open(cast(Cluster, object()), "", "", ScriptedUpstream(), cast(SessionDispatcherClient, object()))

    original: environment_service_pb2.EnvironmentDescriptor | None = None

    async def reused(worker: Worker) -> None:
        nonlocal original
        async with worker.reused_sandbox() as case:
            if original is None:
                original = case.sandbox
            assert case.sandbox is original

    async def fresh(worker: Worker) -> None:
        async with worker.fresh_sandboxes() as case:
            assert original is not None
            assert original.target.uid in deleted
            await case.sandboxes("fresh")

    async def after(worker: Worker) -> None:
        async with worker.reused_sandbox() as case:
            assert original is not None
            assert case.sandbox.target.uid != original.target.uid

    cases = {
        "first": Case("first", reused),
        "second": Case("second", reused),
        "fresh": Case("fresh", fresh),
        "after": Case("after", after),
    }

    async def failed_batch() -> None:
        async with CaseBatch.running(cases, limit=1, worker_scope=worker_scope) as batch:
            assert (await batch.result("first")).error is None
            assert (await batch.result("second")).error is None
            assert isinstance((await batch.result("after")).error, ExceptionGroup)

    if delete_fails:
        with pytest.raises(ExceptionGroup) as aborted:
            await failed_batch()
        assert aborted.value.subgroup(CleanupError) is not None
        assert len(created) == 1
    else:
        async with CaseBatch.running(cases, limit=1, worker_scope=worker_scope) as batch:
            for name in cases:
                assert (await batch.result(name)).error is None
        assert len(created) == 3
    assert deleted == [view.target.uid for view in created]
