"""Ingestion of a runner's events: bounded batches read off one persistent transport read across
flush deadlines, and `Ingestion`, which records each batch under the sandbox's lease."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app import thread_fold
from agentplane.app.thread import event_log, ingestion_lease
from agentplane.app.thread.event_log import EventReplicationError
from agentplane.app.thread.ingestion_lease import IngestionLease
from agentplane.app.thread.recording import ThreadFoldError, record_thread_fold, set_operational
from agentplane.app.thread.updates import notify
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import StreamClosedError

# gazelle:include_dep @pypi//protobuf


async def event_batches(
    read: Callable[[], Awaitable[event_log_pb2.EventEntry]], *, limit: int = 128, delay_s: float = 0.025
) -> AsyncGenerator[list[event_log_pb2.EventEntry]]:
    """Flush by count, elapsed batch delay, or EOF; never cancel a read to flush a batch."""
    if limit < 1 or delay_s <= 0:
        raise ValueError("batch size and delay must be positive")
    loop = asyncio.get_running_loop()
    pending = asyncio.ensure_future(read())
    try:
        while True:
            try:
                batch = [await pending]
            except StreamClosedError:
                return
            deadline = loop.time() + delay_s
            pending = asyncio.ensure_future(read())
            while len(batch) < limit:
                done, _ = await asyncio.wait({pending}, timeout=max(0, deadline - loop.time()))
                if not done:
                    break
                try:
                    batch.append(pending.result())
                except StreamClosedError:
                    yield batch
                    return
                pending = asyncio.ensure_future(read())
            yield batch
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


class Ingestion:
    """Writes under a sandbox's ingestion lease. Each commits the event log's part and the fold's
    part of a runner's events in one transaction, fenced by the lease."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def acquire(self, sandbox: str, duration: timedelta) -> IngestionLease | None:
        async with self._sessions.begin() as session:
            return await ingestion_lease.acquire(session, sandbox, duration)

    async def renew(self, lease: IngestionLease, duration: timedelta) -> bool:
        async with self._sessions.begin() as session:
            return await ingestion_lease.renew(session, lease, duration)

    async def release(self, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.release(session, lease)

    async def record(
        self, thread_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, lease: IngestionLease
    ) -> None:
        """Atomically extend the contiguous prefix, accepting only identical replayed entries."""
        if not entries:
            return
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            inserted = await event_log.append(session, thread_id, entries)
            if not inserted:
                return
            try:
                await record_thread_fold(session, thread_id, inserted[0].origin.source_id, inserted)
            except EventReplicationError:
                raise
            except (thread_fold.ObservationNotUnderstoodError, thread_fold.FoldContractError) as error:
                raise ThreadFoldError(
                    f"thread fold failed at cursor {error.cursor}: {error}", cursor=error.cursor
                ) from error
            except ValueError as error:
                raise ThreadFoldError(f"thread fold failed: {error}") from error
            # The maximum stored cursor is the checkpoint: the fenced transaction admits
            # only a contiguous suffix, so there is no separately mutable progress counter.
            await event_log.advance_feed(session, thread_id, inserted)
            await notify(session)

    async def set_attached(self, thread_id: UUID, attached: protocol_pb2.Attached, *, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            await event_log.set_attached(session, thread_id, attached)
            await set_operational(session, thread_id, status="active", error=None)
            await notify(session)

    async def end_feed(
        self, thread_id: UUID, *, lease: IngestionLease, error: str | None, error_cursor: int | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            await event_log.end_feed(session, thread_id, error)
            await set_operational(
                session,
                thread_id,
                status="ended" if error is None else "failed",
                error=error,
                error_cursor=error_cursor,
            )
            await session.flush()
            await notify(session)
