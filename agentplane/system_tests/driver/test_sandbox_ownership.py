"""Exact-target Sandbox ownership behavior shared by the system-test API adapters."""

import asyncio

import pytest

# gazelle:include_dep @pypi//protobuf
from agentplane.proto.identity_pb2 import EnvironmentTarget
from agentplane.system_tests.driver.sandbox_ownership import release_owned_sandboxes, remember_sandbox


def test_remember_sandbox_keeps_the_acknowledged_incarnation() -> None:
    created = EnvironmentTarget(namespace="test", name="owned-sandbox", uid="owned-uid")
    owned: list[EnvironmentTarget] = []

    remember_sandbox(owned, created)
    created.uid = "replacement-uid"

    assert len(owned) == 1
    assert owned[0] == EnvironmentTarget(namespace="test", name="owned-sandbox", uid="owned-uid")


async def test_release_waits_for_every_target_before_propagating_cancellation() -> None:
    owned = [
        EnvironmentTarget(namespace="test", name="owned-0", uid="uid-0"),
        EnvironmentTarget(namespace="test", name="owned-1", uid="uid-1"),
    ]
    started: set[str] = set()
    finished: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def delete(target: EnvironmentTarget) -> None:
        started.add(target.uid)
        if len(started) == len(owned):
            all_started.set()
        await release.wait()
        finished.add(target.uid)

    closing = asyncio.create_task(release_owned_sandboxes(owned, delete))
    await asyncio.wait_for(all_started.wait(), timeout=5)
    closing.cancel("cancel during Sandbox release")
    assert not closing.done()
    release.set()

    with pytest.raises(asyncio.CancelledError, match="cancel during Sandbox release"):
        await closing

    assert started == {"uid-0", "uid-1"}
    assert finished == started
