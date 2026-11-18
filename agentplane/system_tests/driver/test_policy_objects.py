"""Policy resource scope teardown survives repeated cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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


async def test_policy_scope_preserves_primary_failure_when_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    original_error = ValueError("scenario failed")

    async def kubectl(_cluster: Cluster, *args: str, body: object | None = None) -> str:
        if args[0] == "create":
            assert body is not None
            return ""
        if args[0] == "delete":
            raise RuntimeError("sensitive cleanup details")
        raise AssertionError(args)

    monkeypatch.setattr(Cluster, "kubectl", kubectl)

    async def scenario() -> None:
        async with PolicyObjects(cluster) as policies:
            await policies.create_policy_set("test-policy", [])
            raise original_error

    with pytest.raises(ValueError, match="scenario failed") as raised:
        await scenario()

    assert raised.value is original_error
    assert raised.value.__notes__ == [
        "Policy resource cleanup also failed (1 resource(s)); cleanup details are suppressed to preserve the original failure."
    ]
    assert "sensitive cleanup details" not in str(raised.value.__notes__)


async def test_policy_scope_attempts_all_deletions_and_aggregates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    deletes: list[tuple[str, ...]] = []

    async def kubectl(_cluster: Cluster, *args: str, body: object | None = None) -> str:
        if args[0] == "create":
            assert body is not None
            return ""
        if args[0] == "delete":
            deletes.append(args)
            if args[2] in {"test-policy-a", "test-policy-c"}:
                raise RuntimeError(args[2])
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(Cluster, "kubectl", kubectl)

    async def scenario() -> None:
        async with PolicyObjects(cluster) as policies:
            await policies.create_policy_set("test-policy-a", [])
            await policies.create_policy_set("test-policy-b", [])
            await policies.create_policy_set("test-policy-c", [])

    with pytest.raises(ExceptionGroup, match="Policy resource cleanup failed") as raised:
        await scenario()

    assert deletes == [
        ("delete", "policysets", "test-policy-c", "--ignore-not-found"),
        ("delete", "policysets", "test-policy-b", "--ignore-not-found"),
        ("delete", "policysets", "test-policy-a", "--ignore-not-found"),
    ]
    assert [str(error) for error in raised.value.exceptions] == ["test-policy-c", "test-policy-a"]
