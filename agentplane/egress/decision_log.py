"""Bounded best-effort persistence; admission never awaits database IO."""

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from agentplane.egress.decision_store import DecisionStore
from agentplane.egress.decisions import DecisionRecord

logger = logging.getLogger(__name__)
DB_ERRORS = (SQLAlchemyError, TimeoutError, OSError)


@dataclass
class Diagnostics:
    accepted: int = 0
    acknowledged: int = 0
    dropped_overflow: int = 0
    dropped_unavailable: int = 0
    dropped_expired: int = 0
    dropped_shutdown: int = 0
    write_failures: int = 0
    cleanup_failures: int = 0
    available: bool | None = None


class DecisionLog:
    def __init__(self, store: DecisionStore, *, queue_size: int = 2000, batch_size: int = 100) -> None:
        if queue_size < 1 or batch_size < 1:
            raise ValueError("queue and batch sizes must be positive")
        self.store = store
        self._queue: asyncio.Queue[DecisionRecord] = asyncio.Queue(queue_size)
        self._batch_size = batch_size
        self._closing = False
        self._worker: asyncio.Task[None] | None = None
        self.diagnostics = Diagnostics()

    def record(self, decision: DecisionRecord) -> None:
        if self._closing:
            self.diagnostics.dropped_shutdown += 1
            logger.warning("decision history dropped: shutting down")
            return
        try:
            self._queue.put_nowait(decision)
        except asyncio.QueueFull:
            self.diagnostics.dropped_overflow += 1
            logger.warning("decision history dropped: queue full")
            return
        self.diagnostics.accepted += 1
        logger.info("%s", decision.model_dump_json())

    def health(self) -> dict[str, int | bool | None]:
        return {
            **asdict(self.diagnostics),
            "queued": self._queue.qsize(),
            "writer_running": self._worker is not None and not self._worker.done(),
        }

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("decision writer already started")
        self._worker = asyncio.create_task(self._run(), name="egress-decision-writer")
        self._worker.add_done_callback(self._finished)

    def _finished(self, worker: asyncio.Task[None]) -> None:
        if not worker.cancelled() and (error := worker.exception()) is not None:
            self.diagnostics.available = False
            logger.error("decision history writer stopped (%s)", type(error).__name__)

    async def flush(self) -> None:
        await self._queue.join()

    async def close(self, flush_seconds: float = 5) -> None:
        self._closing = True
        try:
            async with asyncio.timeout(flush_seconds):
                await self.flush()
        except TimeoutError:
            logger.warning("decision history shutdown deadline reached")
        finally:
            if self._worker is not None:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
                self.diagnostics.dropped_shutdown += 1
            await self.store.engine.dispose()

    async def _write(self, batch: list[DecisionRecord]) -> None:
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(DB_ERRORS),
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                reraise=True,
            ):
                with attempt:
                    try:
                        await self.store.append(batch)
                    except DB_ERRORS as error:
                        self.diagnostics.write_failures += 1
                        self.diagnostics.available = False
                        # Never log SQL parameters, connection URLs, or driver exception messages.
                        logger.warning("decision history write failed (%s)", type(error).__name__)
                        raise
            self.diagnostics.available = True
            self.diagnostics.acknowledged += len(batch)
        except DB_ERRORS:
            self.diagnostics.dropped_unavailable += len(batch)
            logger.warning("decision history dropped: retry budget exhausted (%d records)", len(batch))

    async def _run(self) -> None:
        while True:
            batch: list[DecisionRecord] = []
            pending = 0
            try:
                try:
                    async with asyncio.timeout(60):
                        batch.append(await self._queue.get())
                except TimeoutError:
                    pass
                while len(batch) < self._batch_size and not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                cutoff = datetime.now(UTC) - self.store.retention
                current = [record for record in batch if record.at >= cutoff]
                self.diagnostics.dropped_expired += len(batch) - len(current)
                pending = len(current)
                if current:
                    await self._write(current)
            except asyncio.CancelledError:
                self.diagnostics.dropped_shutdown += pending
                raise
            finally:
                for _ in batch:
                    self._queue.task_done()
            try:
                await self.store.cleanup()
            except DB_ERRORS as error:
                self.diagnostics.cleanup_failures += 1
                logger.warning("decision history cleanup failed (%s)", type(error).__name__)
