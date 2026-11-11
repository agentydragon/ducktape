"""Bounded work queue whose completed jobs can add more jobs."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

DEFAULT_WORKERS = 8


@dataclass(frozen=True, slots=True)
class Finished[T]:
    value: T
    outcome: str


@dataclass(frozen=True, slots=True)
class WorkFailure:
    error: Exception


@dataclass(frozen=True, slots=True)
class Cancelled:
    pass


class ThreadWorkItem[T](Protocol):
    @property
    def phase(self) -> str: ...

    def run(self) -> Finished[T]: ...


class AsyncWorkItem[T]:
    """Cancellable read-only work; mutations use `ThreadWorkItem` and finish once admitted."""

    phase: ClassVar[str]

    async def run_async(self) -> Finished[T]:
        raise NotImplementedError


type WorkItem[T] = ThreadWorkItem[T] | AsyncWorkItem[T]


@dataclass(slots=True)
class Phase:
    ready: int = 0
    running: int = 0
    finished: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)


class WorkQueue[T]:
    """The event loop owns the queue; blocking jobs run on at most `workers` threads.

    A bounded preference for follow-up work lets removals start promptly while discovery
    continues. Job results return to the owner, which can enqueue further work before the
    next batch starts. No queued jobs start after `stop`.
    """

    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        self.workers = workers
        self.phases: dict[str, Phase] = {}
        self._ready: deque[WorkItem[T]] = deque()
        self._followups: deque[WorkItem[T]] = deque()
        self._running: dict[asyncio.Task[Finished[T] | WorkFailure | Cancelled], WorkItem[T]] = {}
        self._followup_streak = 0
        self._stopped = False
        self._start_lock = threading.Lock()

    def submit(self, item: WorkItem[T], *, followup: bool = False) -> None:
        if self._stopped:
            raise RuntimeError("work queue has stopped")
        self.phases.setdefault(item.phase, Phase()).ready += 1
        (self._followups if followup else self._ready).append(item)

    def __bool__(self) -> bool:
        return bool(self._ready or self._followups or self._running)

    def start(self, tasks: asyncio.TaskGroup) -> list[WorkItem[T]]:
        started: list[WorkItem[T]] = []
        while len(self._running) < self.workers and (self._ready or self._followups):
            if self._followups and (self._followup_streak < 3 or not self._ready):
                item = self._followups.popleft()
                self._followup_streak += 1
            else:
                item = self._ready.popleft()
                self._followup_streak = 0
            phase = self.phases[item.phase]
            phase.ready -= 1
            phase.running += 1
            task = tasks.create_task(self._execute(item))
            self._running[task] = item
            started.append(item)
        return started

    def _run(self, item: ThreadWorkItem[T]) -> Finished[T] | Cancelled:
        with self._start_lock:
            if self._stopped:
                return Cancelled()
        return item.run()

    async def _run_async(self, item: AsyncWorkItem[T]) -> Finished[T] | Cancelled:
        with self._start_lock:
            if self._stopped:
                return Cancelled()
        return await item.run_async()

    async def _execute(self, item: WorkItem[T]) -> Finished[T] | WorkFailure | Cancelled:
        # Keep job failures as results so TaskGroup does not cancel admitted mutations.
        try:
            if isinstance(item, AsyncWorkItem):
                return await self._run_async(item)
            return await asyncio.to_thread(self._run, item)
        except Exception as error:
            return WorkFailure(error)

    async def next(self) -> tuple[WorkItem[T], Finished[T] | WorkFailure]:
        if not self._running:
            raise RuntimeError("work queue has no running jobs")
        done, _ = await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)
        task = next(iter(done))
        item = self._running.pop(task)
        phase = self.phases[item.phase]
        phase.running -= 1
        phase.finished += 1
        result = task.result()
        if isinstance(result, WorkFailure):
            phase.outcomes["failed"] += 1
            return item, result
        assert isinstance(result, Finished)
        phase.outcomes[result.outcome] += 1
        return item, result

    def stop(self) -> list[WorkItem[T]]:
        """Cancel unstarted and asynchronous read-only jobs; admitted mutations finish."""
        with self._start_lock:
            self._stopped = True
        for task, item in self._running.items():
            if isinstance(item, AsyncWorkItem):
                task.cancel()
        cancelled = [*self._ready, *self._followups]
        for item in cancelled:
            self.phases[item.phase].ready -= 1
        self._ready.clear()
        self._followups.clear()
        return cancelled

    async def drain(self) -> list[tuple[WorkItem[T], Finished[T] | WorkFailure | Cancelled]]:
        """Collect jobs already running after `stop`."""
        completed: list[tuple[WorkItem[T], Finished[T] | WorkFailure | Cancelled]] = []
        while self._running:
            done, _ = await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                item = self._running.pop(task)
                phase = self.phases[item.phase]
                phase.running -= 1
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    completed.append((item, Cancelled()))
                else:
                    if isinstance(result, Cancelled):
                        completed.append((item, result))
                        continue
                    phase.finished += 1
                    phase.outcomes["failed" if isinstance(result, WorkFailure) else result.outcome] += 1
                    completed.append((item, result))
        return completed
