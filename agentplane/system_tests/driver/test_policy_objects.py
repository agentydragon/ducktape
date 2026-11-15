"""Policy resource scope teardown survives repeated cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.policy_objects import PolicyObjects


async def test_policy_scope_finishes_deletion_before_propagating_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    resources_created = asyncio.Event()
    deleting = asyncio.Event()
    finish_deletion = asyncio.Event()
    deleted = asyncio.Event()
    operations: list[tuple[str, ...]] = []

    async def kubectl(_cluster: Cluster, *args: str, body: object | None = None) -> str:
        operations.append(args)
        match args[0]:
            case "create":
                assert body is not None
            case "delete":
                deleting.set()
                await finish_deletion.wait()
                deleted.set()
            case _:
                raise AssertionError(args)
        return ""

    monkeypatch.setattr(Cluster, "kubectl", kubectl)

    async def case() -> None:
        async with PolicyObjects(cluster) as policies:
            await policies.create_policy_set("test-policy", [])
            resources_created.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(case())
    await resources_created.wait()
    task.cancel()
    await deleting.wait()
    task.cancel()

    cancellation_delivered = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(cancellation_delivered.set_result, None)
    await cancellation_delivered
    assert not task.done()

    finish_deletion.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert deleted.is_set()
    assert operations == [("create", "-f", "-"), ("delete", "policysets", "test-policy", "--ignore-not-found")]


if __name__ == "__main__":
    pytest_bazel.main()
