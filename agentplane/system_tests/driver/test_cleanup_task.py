"""Cleanup task ownership under repeated cancellation and independent cleanup failure."""

import asyncio

import pytest
import pytest_bazel

from agentplane.system_tests.driver.cleanup_task import finish_cleanup


@pytest.mark.parametrize("fails", [False, True])
async def test_cleanup_drains_after_each_cancellation_and_keeps_failure_cause(fails: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    failure = RuntimeError("cleanup failed")

    async def cleanup() -> int:
        started.set()
        await release.wait()
        completed.set()
        if fails:
            raise failure
        return 17

    owner = asyncio.create_task(finish_cleanup(cleanup()))
    await started.wait()
    for _ in range(3):
        owner.cancel()
        delivered = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(delivered.set_result, None)
        await delivered
        assert not owner.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await owner
    assert completed.is_set()
    assert caught.value.__cause__ is (failure if fails else None)


async def test_cleanup_propagates_its_own_cancellation_without_waiting_forever() -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError):
            await finish_cleanup(cancelled())


async def test_late_cancellation_does_not_replace_an_existing_body_failure() -> None:
    original = AssertionError("scenario failed")
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()

    async def scenario() -> None:
        try:
            raise original
        finally:
            await finish_cleanup(cleanup(), primary_error=original)

    owner = asyncio.create_task(scenario())
    await started.wait()
    owner.cancel()
    release.set()
    with pytest.raises(AssertionError) as caught:
        await owner
    assert caught.value is original


if __name__ == "__main__":
    pytest_bazel.main()
