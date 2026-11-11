import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import ClassVar

import pytest
import pytest_bazel

from devinfra.gc.work_queue import AsyncMutation, AsyncWorkItem, Cancelled, Finished, WorkFailure, WorkQueue


@dataclass(frozen=True)
class Job:
    name: str
    phase: str
    started: threading.Event
    release: threading.Event

    def run(self) -> Finished[str]:
        self.started.set()
        assert self.release.wait(10)
        return Finished(self.name, "done")


@dataclass(frozen=True)
class AsyncDiscovery(AsyncWorkItem[str]):
    started: asyncio.Event
    release: asyncio.Event
    phase: ClassVar[str] = "discover"

    async def run_async(self) -> Finished[str]:
        self.started.set()
        await self.release.wait()
        return Finished("network discovery", "done")


@pytest.fixture
async def tasks() -> AsyncIterator[asyncio.TaskGroup]:
    async with asyncio.TaskGroup() as group:
        yield group


async def test_async_discovery_does_not_block_independent_work(tasks: asyncio.TaskGroup) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    immediate = threading.Event()
    immediate.set()
    queue = WorkQueue[str](workers=2)
    queue.submit(AsyncDiscovery(started, release))
    queue.submit(Job("filesystem scan", "scan", threading.Event(), immediate))
    try:
        queue.start(tasks)
        await asyncio.wait_for(started.wait(), 10)
        _, finished = await asyncio.wait_for(queue.next(), 10)
        assert isinstance(finished, Finished)
        assert finished.value == "filesystem scan"
    finally:
        queue.stop()
        await queue.drain()


async def test_followup_can_run_while_another_discovery_is_in_progress(tasks: asyncio.TaskGroup) -> None:
    immediate = threading.Event()
    immediate.set()
    slow_started, slow_release, deleted = threading.Event(), threading.Event(), threading.Event()
    queue = WorkQueue[str](workers=2)
    queue.submit(Job("first", "scan", threading.Event(), immediate))
    queue.submit(Job("slow", "scan", slow_started, slow_release))
    try:
        queue.start(tasks)
        assert await asyncio.to_thread(slow_started.wait, 10)
        _, finished = await asyncio.wait_for(queue.next(), 10)
        assert not isinstance(finished, WorkFailure)
        assert finished.value == "first"
        assert queue.phases["scan"].running == 1
        assert queue.phases["scan"].finished == 1
        queue.submit(Job("delete", "remove", deleted, immediate), followup=True)
        queue.start(tasks)
        assert await asyncio.to_thread(deleted.wait, 10)
        assert not slow_release.is_set()
    finally:
        slow_release.set()
        queue.stop()
        await queue.drain()


async def test_stop_does_not_start_queued_work(tasks: asyncio.TaskGroup) -> None:
    started, release, queued_started = threading.Event(), threading.Event(), threading.Event()
    queue = WorkQueue[str](workers=1)
    queue.submit(Job("running", "scan", started, release))
    queue.submit(Job("queued", "scan", queued_started, release))
    queue.start(tasks)
    assert await asyncio.to_thread(started.wait, 10)
    queue.stop()
    release.set()
    finished = await asyncio.wait_for(queue.drain(), 10)
    assert len(finished) == 1
    _, result = finished[0]
    assert isinstance(result, Finished)
    assert result.value == "running"
    assert not queued_started.is_set()
    assert queue.phases["scan"].ready == 0
    assert queue.phases["scan"].finished == 2
    assert queue.phases["scan"].outcomes["cancelled"] == 1


async def test_stop_drains_an_admitted_async_mutation(tasks: asyncio.TaskGroup) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Mutation(AsyncMutation[str]):
        phase = "remove"

        async def run_async(self) -> Finished[str]:
            started.set()
            await release.wait()
            return Finished("removed", "done")

    queue = WorkQueue[str](workers=1)
    queue.submit(Mutation())
    queue.start(tasks)
    await asyncio.wait_for(started.wait(), 10)
    queue.stop()
    release.set()
    [(_, result)] = await asyncio.wait_for(queue.drain(), 10)
    assert isinstance(result, Finished)
    assert result.value == "removed"


async def test_stop_prevents_scheduled_threads_from_starting_work(tasks: asyncio.TaskGroup) -> None:
    started, release = threading.Event(), threading.Event()
    queue = WorkQueue[str](workers=1)
    queue.submit(Job("scheduled", "scan", started, release))
    queue.start(tasks)
    queue.stop()
    drained = await queue.drain()
    assert len(drained) == 1
    assert isinstance(drained[0][1], Cancelled)
    assert not started.is_set()
    assert queue.phases["scan"].finished == 1
    assert queue.phases["scan"].outcomes["cancelled"] == 1


async def test_stop_drains_thread_work_admitted_before_executor_starts(
    tasks: asyncio.TaskGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted, allow_start = asyncio.Event(), asyncio.Event()
    started, release = threading.Event(), threading.Event()
    release.set()
    to_thread = asyncio.to_thread

    async def delayed_submission[**P, T](func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        submitted.set()
        await allow_start.wait()
        return await to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", delayed_submission)
    queue = WorkQueue[str](workers=1)
    queue.submit(Job("admitted", "remove", started, release))
    queue.start(tasks)
    await asyncio.wait_for(submitted.wait(), 10)
    queue.stop()
    allow_start.set()
    [(_, result)] = await asyncio.wait_for(queue.drain(), 10)
    assert isinstance(result, Finished)
    assert started.is_set()


async def test_stop_before_async_mutation_starts_does_not_admit_it(tasks: asyncio.TaskGroup) -> None:
    started = asyncio.Event()

    class Mutation(AsyncMutation[str]):
        phase = "remove"

        async def run_async(self) -> Finished[str]:
            started.set()
            return Finished("removed", "done")

    queue = WorkQueue[str](workers=1)
    queue.submit(Mutation())
    queue.start(tasks)
    queue.stop()
    [(_, result)] = await queue.drain()
    assert isinstance(result, Cancelled)
    assert not started.is_set()


@pytest.mark.parametrize("error", [ValueError("scan failed"), KeyboardInterrupt("worker interrupted")])
async def test_job_failure_does_not_cancel_an_admitted_mutation(tasks: asyncio.TaskGroup, error: BaseException) -> None:
    mutation_started, mutation_release, trigger_failure = threading.Event(), threading.Event(), threading.Event()

    class Fail:
        phase = "scan"

        def run(self) -> Finished[str]:
            assert trigger_failure.wait(10)
            raise error

    queue = WorkQueue[str](workers=2)
    queue.submit(Job("mutation", "remove", mutation_started, mutation_release))
    queue.submit(Fail())
    try:
        queue.start(tasks)
        assert await asyncio.to_thread(mutation_started.wait, 10)
        trigger_failure.set()
        _, failed = await asyncio.wait_for(queue.next(), 10)
        assert isinstance(failed, WorkFailure)
        queue.stop()
        mutation_release.set()
        [(_, completed)] = await asyncio.wait_for(queue.drain(), 10)
        assert isinstance(completed, Finished)
        assert completed.value == "mutation"
    finally:
        trigger_failure.set()
        mutation_release.set()
        queue.stop()
        await queue.drain()


if __name__ == "__main__":
    pytest_bazel.main()
