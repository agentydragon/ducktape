"""The batch owns selected case work through reporting, failure and cancellation."""

import asyncio
from functools import partial

import pytest
import pytest_bazel
from opentelemetry import trace

from agentplane.system_tests.driver import case_batch
from agentplane.system_tests.driver.case_batch import Case, CaseBatch, Result


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


@pytest.mark.parametrize(
    "outcome_type",
    [pytest.fail.Exception, pytest.skip.Exception, pytest.xfail.Exception],
    ids=["fail", "skip", "xfail"],
)
async def test_nested_outcome_group_is_delivered_before_peer_finishes(outcome_type: type[BaseException]) -> None:
    peer_started = asyncio.Event()
    fail_now = asyncio.Event()
    release_peer = asyncio.Event()
    peer_finished = asyncio.Event()
    cleanup_drained = asyncio.Event()

    async def bad() -> None:
        await fail_now.wait()
        nested = BaseExceptionGroup("inner", [outcome_type("pytest control outcome"), ValueError("ordinary failure")])
        raise BaseExceptionGroup("outer", [nested])

    async def peer() -> None:
        try:
            peer_started.set()
            await release_peer.wait()
            peer_finished.set()
        finally:
            cleanup_drained.set()

    cases = {"bad": Case("bad", bad), "peer": Case("peer", peer)}
    async with asyncio.timeout(2), CaseBatch.running(cases, limit=2, exclusive=None) as batch:
        await peer_started.wait()
        fail_now.set()
        result = await batch.result("bad")
        assert isinstance(result.error, BaseExceptionGroup)
        inner = result.error.exceptions[0]
        assert isinstance(inner, BaseExceptionGroup)
        assert isinstance(inner.exceptions[0], outcome_type)
        assert isinstance(inner.exceptions[1], ValueError)
        assert not peer_finished.is_set()
        release_peer.set()
        await peer_finished.wait()
        assert (await batch.result("peer")).error is None
    assert cleanup_drained.is_set()


async def test_mixed_base_exception_group_wakes_waiter_and_propagates_original() -> None:
    class Fatal(BaseException):
        pass

    peer_started = asyncio.Event()
    fail_now = asyncio.Event()
    waiter_started = asyncio.Event()
    cleanup_drained = asyncio.Event()

    async def bad() -> None:
        await fail_now.wait()
        nested = BaseExceptionGroup(
            "inner", [pytest.fail.Exception("pytest control outcome"), ValueError("ordinary failure")]
        )
        raise BaseExceptionGroup("outer", [nested, Fatal("fatal leaf")])

    async def peer() -> None:
        peer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_drained.set()

    async def wait_for_result(batch: CaseBatch) -> Result:
        waiter_started.set()
        return await batch.result("bad")

    cases = {"bad": Case("bad", bad), "peer": Case("peer", peer)}
    result_holder: list[Result] = []

    async def run_batch() -> None:
        async with asyncio.timeout(2), CaseBatch.running(cases, limit=2, exclusive=None) as batch:
            await peer_started.wait()
            waiter = asyncio.create_task(wait_for_result(batch))
            await waiter_started.wait()
            fail_now.set()
            result_holder.append(await waiter)
            assert cleanup_drained.is_set()

    with pytest.raises(BaseExceptionGroup) as propagated:
        await run_batch()

    assert len(result_holder) == 1
    result_error = result_holder[0].error
    assert isinstance(result_error, BaseExceptionGroup)
    assert isinstance(propagated.value, BaseExceptionGroup)
    for error in (result_error, propagated.value):
        assert error.subgroup(pytest.fail.Exception) is not None
        assert error.subgroup(ValueError) is not None
        assert error.subgroup(Fatal) is not None
    assert cleanup_drained.is_set()


async def test_caller_cancellation_propagates_and_drains_case_cleanup() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    result_waiting = asyncio.Event()
    batch_holder: list[CaseBatch] = []

    async def active() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def wait_for_result(batch: CaseBatch) -> Result:
        result_waiting.set()
        return await batch.result("active")

    async def use_batch() -> None:
        async with CaseBatch.running({"active": Case("active", active)}, limit=1, exclusive=None) as batch:
            batch_holder.append(batch)
            await started.wait()
            pending_result = asyncio.create_task(wait_for_result(batch))
            await result_waiting.wait()
            pending_result.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_result
            raise asyncio.CancelledError

    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError):
            await use_batch()
    assert cleaned.is_set()
    result_waiting.clear()
    pending_result = asyncio.create_task(wait_for_result(batch_holder[0]))
    await result_waiting.wait()
    assert not pending_result.done()
    pending_result.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_result


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
