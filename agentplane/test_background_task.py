import asyncio
import logging

import pytest
import pytest_bazel

from agentplane.background_task import background


async def test_background_failure_is_logged_and_the_block_continues(caplog: pytest.LogCaptureFixture) -> None:
    failed = asyncio.Event()

    async def fail() -> None:
        failed.set()
        raise RuntimeError("informer lost")

    with caplog.at_level(logging.ERROR):
        async with background(fail(), name="informer"):
            await failed.wait()
            await asyncio.sleep(0)
            continued = True
    assert continued
    assert caplog.records[0].exc_info is not None
    assert isinstance(caplog.records[0].exc_info[1], RuntimeError)


async def test_background_is_cancelled_when_the_block_exits() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with background(run(), name="watch"):
        await started.wait()
    assert cancelled.is_set()


if __name__ == "__main__":
    pytest_bazel.main()
