import asyncio
import threading
from dataclasses import dataclass
from typing import ClassVar

import pytest_bazel

from devinfra.gc.work_queue import AsyncWorkItem, Cancelled, Finished, WorkFailure, WorkQueue


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


async def test_async_discovery_does_not_block_independent_work() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    immediate = threading.Event()
    immediate.set()
    queue = WorkQueue[str](workers=2)
    queue.submit(AsyncDiscovery(started, release))
    queue.submit(Job("filesystem scan", "scan", threading.Event(), immediate))
    try:
        queue.start()
        await asyncio.wait_for(started.wait(), 10)
        _, finished = await asyncio.wait_for(queue.next(), 10)
        assert isinstance(finished, Finished)
        assert finished.value == "filesystem scan"
    finally:
        queue.stop()
        await queue.drain()


async def test_followup_can_run_while_another_discovery_is_in_progress() -> None:
    immediate = threading.Event()
    immediate.set()
    slow_started, slow_release, deleted = threading.Event(), threading.Event(), threading.Event()
    queue = WorkQueue[str](workers=2)
    queue.submit(Job("first", "scan", threading.Event(), immediate))
    queue.submit(Job("slow", "scan", slow_started, slow_release))
    try:
        queue.start()
        assert await asyncio.to_thread(slow_started.wait, 10)
        _, finished = await asyncio.wait_for(queue.next(), 10)
        assert not isinstance(finished, WorkFailure)
        assert finished.value == "first"
        assert queue.phases["scan"].running == 1
        assert queue.phases["scan"].finished == 1
        queue.submit(Job("delete", "remove", deleted, immediate), followup=True)
        queue.start()
        assert await asyncio.to_thread(deleted.wait, 10)
        assert not slow_release.is_set()
    finally:
        slow_release.set()
        queue.stop()
        await queue.drain()


async def test_stop_does_not_start_queued_work() -> None:
    started, release, queued_started = threading.Event(), threading.Event(), threading.Event()
    queue = WorkQueue[str](workers=1)
    queue.submit(Job("running", "scan", started, release))
    queue.submit(Job("queued", "scan", queued_started, release))
    queue.start()
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
    assert queue.phases["scan"].finished == 1


async def test_stop_prevents_scheduled_threads_from_starting_work() -> None:
    started, release = threading.Event(), threading.Event()
    queue = WorkQueue[str](workers=1)
    queue.submit(Job("scheduled", "scan", started, release))
    queue.start()
    queue.stop()
    drained = await queue.drain()
    assert len(drained) == 1
    assert isinstance(drained[0][1], Cancelled)
    assert not started.is_set()
    assert queue.phases["scan"].finished == 0


if __name__ == "__main__":
    pytest_bazel.main()
