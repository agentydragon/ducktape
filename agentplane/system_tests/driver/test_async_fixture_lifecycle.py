"""Library spike: AnyIO keeps scoped task groups in one task but does not shield raw Task.cancel()."""

import asyncio
from collections.abc import AsyncIterator

import anyio
import pytest
import pytest_bazel

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def owning_task(anyio_backend: str) -> AsyncIterator[asyncio.Task[object]]:
    del anyio_backend
    task = asyncio.current_task()
    assert task is not None
    async with anyio.create_task_group() as workers:
        workers.start_soon(anyio.sleep_forever)
        yield task
        # Exiting this scope in pytest-asyncio's separate finalizer task fails.
        assert asyncio.current_task() is task
        workers.cancel_scope.cancel()


async def test_scoped_fixture_and_test_use_the_same_task(owning_task: asyncio.Task[object]) -> None:
    assert asyncio.current_task() is owning_task


async def test_next_test_keeps_the_same_scoped_task(owning_task: asyncio.Task[object]) -> None:
    assert asyncio.current_task() is owning_task


async def test_anyio_shield_does_not_replace_raw_asyncio_cleanup_protection() -> None:
    started = asyncio.Event()
    completed = asyncio.Event()

    async def cleanup() -> None:
        with anyio.CancelScope(shield=True):
            started.set()
            await asyncio.Event().wait()
            completed.set()

    task = asyncio.create_task(cleanup())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not completed.is_set()


if __name__ == "__main__":
    pytest_bazel.main()
