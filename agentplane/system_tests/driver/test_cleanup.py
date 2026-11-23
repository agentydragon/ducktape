"""Cancellation-safe cleanup completion behavior."""

import asyncio

import pytest

from agentplane.system_tests.driver.cleanup import finish_cleanup


async def test_finish_cleanup_reports_child_cancellation_as_a_failure() -> None:
    async def cleanup() -> None:
        raise asyncio.CancelledError("cleanup task stopped")

    result = await finish_cleanup(cleanup())

    assert isinstance(result.failure, asyncio.CancelledError)
    assert result.cancellation is None


@pytest.mark.parametrize("fails", [False, True])
async def test_finish_cleanup_drains_repeated_cancellation_and_reports_both_outcomes(fails: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    failure = RuntimeError("cleanup failed")

    async def cleanup() -> None:
        started.set()
        await release.wait()
        completed.set()
        if fails:
            raise failure

    owner = asyncio.create_task(finish_cleanup(cleanup()))
    await started.wait()
    for _ in range(3):
        owner.cancel()
        delivered = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(delivered.set_result, None)
        await delivered
        assert not owner.done()
    release.set()
    result = await owner
    assert completed.is_set()
    assert isinstance(result.cancellation, asyncio.CancelledError)
    assert result.failure is (failure if fails else None)


async def test_previous_caller_cancellation_is_not_confused_with_child_cancellation() -> None:
    async def child() -> None:
        raise asyncio.CancelledError("cleanup stopped")

    async def caller() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        result = await finish_cleanup(child())
        assert isinstance(result.failure, asyncio.CancelledError)
        assert result.cancellation is None

    async with asyncio.timeout(2):
        await asyncio.create_task(caller())
