"""The batch owns selected case work through reporting, failure and cancellation."""

import asyncio
from functools import partial

import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.system_tests import case_batch
from agentplane.system_tests.case_batch import Case, CaseBatch


async def test_exclusive_case_finishes_before_two_slots_open() -> None:
    exclusive_started = asyncio.Event()
    exclusive_release = asyncio.Event()
    pair_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0
    finished: set[str] = set()

    async def exclusive() -> None:
        exclusive_started.set()
        await exclusive_release.wait()
        finished.add("storage")

    async def peer(name: str) -> None:
        nonlocal active, peak
        assert "storage" in finished
        active += 1
        peak = max(peak, active)
        if active == 2:
            pair_started.set()
        await release.wait()
        active -= 1
        finished.add(name)

    cases = {"storage": Case("storage", exclusive)} | {
        name: Case(name, partial(peer, name)) for name in ["a", "b", "c"]
    }
    async with asyncio.timeout(2), CaseBatch.running(cases, limit=2, exclusive="storage") as batch:
        await exclusive_started.wait()
        assert active == 0
        exclusive_release.set()
        await pair_started.wait()
        assert active == 2
        release.set()
        for name in cases:
            assert (await batch.result(name)).error is None
    assert finished == set(cases)
    assert peak == 2


async def test_case_failure_does_not_cancel_its_peer() -> None:
    peer_started = asyncio.Event()
    peer_finished = asyncio.Event()

    async def bad() -> None:
        await peer_started.wait()
        raise AssertionError("case-owned failure")

    async def peer() -> None:
        peer_started.set()
        await peer_finished.wait()

    cases = {"bad": Case("bad", bad), "peer": Case("peer", peer)}
    async with asyncio.timeout(2), CaseBatch.running(cases, limit=2, exclusive=None) as batch:
        result = await batch.result("bad")
        assert isinstance(result.error, AssertionError)
        peer_finished.set()
        assert (await batch.result("peer")).error is None


async def test_exclusive_failure_does_not_run_other_cases() -> None:
    peers: list[str] = []

    async def bad() -> None:
        raise AssertionError("storage failed")

    async def peer() -> None:
        peers.append("peer")

    cases = {"storage": Case("storage", bad), "peer": Case("peer", peer)}
    async with asyncio.timeout(2), CaseBatch.running(cases, limit=2, exclusive="storage") as batch:
        assert isinstance((await batch.result("storage")).error, AssertionError)
        assert isinstance((await batch.result("peer")).error, RuntimeError)
    assert not peers


async def test_early_exit_cancels_and_drains_active_cleanup() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    queued_started = asyncio.Event()

    async def active() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def queued() -> None:
        queued_started.set()

    cases = {"active": Case("active", active), "queued": Case("queued", queued)}
    async with asyncio.timeout(2):
        async with CaseBatch.running(cases, limit=1, exclusive=None):
            await started.wait()
        assert cleaned.is_set()
        assert not queued_started.is_set()


async def test_driver_failure_wakes_result_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(case_batch, "tracer", trace.NoOpTracer())

    async def run() -> None:
        pass

    async def check() -> None:
        async with CaseBatch.running({"case": Case("case", run)}, limit=1, exclusive=None) as batch:
            error = (await batch.result("case")).error
            assert isinstance(error, ExceptionGroup)

    async with asyncio.timeout(2):
        with pytest.raises(ExceptionGroup):
            await check()


if __name__ == "__main__":
    pytest_bazel.main()
