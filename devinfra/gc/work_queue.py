"""Bounded work queue whose completed jobs can add more jobs."""

from __future__ import annotations

import asyncio
from collections import Counter
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
    """Cancellable read-only work."""

    phase: ClassVar[str]

    async def run_async(self) -> Finished[T]:
        raise NotImplementedError


class AsyncMutation[T](AsyncWorkItem[T]):
    """An admitted mutation must finish and report its outcome during shutdown."""


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
    next batch starts. A scheduled job is admitted when it passes the start check, not
    when its task is created. No queued jobs are admitted after `stop`.
    """

    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        self.workers = workers
        self.phases: dict[str, Phase] = {}
        # Only the coordinator feeds these queues. Workers return discoveries to it, so
        # workers cannot all deadlock trying to enqueue children into a full queue.
        self._ready: asyncio.Queue[WorkItem[T]] = asyncio.Queue()
        self._followups: asyncio.Queue[WorkItem[T]] = asyncio.Queue()
        self._running: dict[asyncio.Task[Finished[T] | WorkFailure | Cancelled], WorkItem[T]] = {}
        self._followup_streak = 0
        self._stopped = False

    def submit(self, item: WorkItem[T], *, followup: bool = False) -> None:
        if self._stopped:
            raise RuntimeError("work queue has stopped")
        self.phases.setdefault(item.phase, Phase()).ready += 1
        (self._followups if followup else self._ready).put_nowait(item)

    def __bool__(self) -> bool:
        return not self._ready.empty() or not self._followups.empty() or bool(self._running)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def start(self, tasks: asyncio.TaskGroup) -> list[WorkItem[T]]:
        started: list[WorkItem[T]] = []
        while (
            not self._stopped
            and len(self._running) < self.workers
            and (not self._ready.empty() or not self._followups.empty())
        ):
            if not self._followups.empty() and (self._followup_streak < 3 or self._ready.empty()):
                item = self._followups.get_nowait()
                self._followups.task_done()
                self._followup_streak += 1
            else:
                item = self._ready.get_nowait()
                self._ready.task_done()
                self._followup_streak = 0
            phase = self.phases[item.phase]
            phase.ready -= 1
            phase.running += 1
            task = tasks.create_task(self._execute(item))
            self._running[task] = item
            started.append(item)
        return started

    async def _execute(self, item: WorkItem[T]) -> Finished[T] | WorkFailure | Cancelled:
        # Keep job failures as results so TaskGroup does not cancel admitted mutations.
        try:
            # Admission and stop are both owned by the event loop. There is no await
            # between this check and submitting blocking work to the executor.
            if self._stopped:
                return Cancelled()
            if isinstance(item, AsyncWorkItem):
                return await item.run_async()
            return await asyncio.to_thread(item.run)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return WorkFailure(error)
        except BaseException as error:
            # A child task must not let TaskGroup tear down an admitted mutation.
            return WorkFailure(RuntimeError(f"{type(error).__name__}: {error}"))

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
        self._stopped = True
        for task, item in self._running.items():
            if isinstance(item, AsyncWorkItem) and not isinstance(item, AsyncMutation):
                task.cancel()
        cancelled: list[WorkItem[T]] = []
        for ready in (self._ready, self._followups):
            while not ready.empty():
                cancelled.append(ready.get_nowait())
                ready.task_done()
        for item in cancelled:
            phase = self.phases[item.phase]
            phase.ready -= 1
            phase.finished += 1
            phase.outcomes["cancelled"] += 1
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
                    phase.finished += 1
                    phase.outcomes["cancelled"] += 1
                    completed.append((item, Cancelled()))
                else:
                    if isinstance(result, Cancelled):
                        phase.finished += 1
                        phase.outcomes["cancelled"] += 1
                        completed.append((item, result))
                        continue
                    phase.finished += 1
                    phase.outcomes["failed" if isinstance(result, WorkFailure) else result.outcome] += 1
                    completed.append((item, result))
        return completed
