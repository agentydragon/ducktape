"""Ingestion of runners' events: bounded batches read off one persistent transport read across flush
deadlines; `Ingestion`, which records each batch under the sandbox's lease; a `Feed` copying one
runner session; and the `Ingester`, which holds the leases and runs the feeds."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import timedelta
from uuid import UUID

import grpc
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app import thread_fold
from agentplane.app.agent_runtime.events import event_log, ingestion_lease
from agentplane.app.agent_runtime.events.event_log import EventLogStore, EventReplicationError, FeedError
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease, IngestionLeaseLostError
from agentplane.app.agent_runtime.runner.runners import Runners, SandboxNotReachableError
from agentplane.app.agent_runtime.updates import notify
from agentplane.app.inventory import SandboxNotFoundError
from agentplane.app.thread.recording import ThreadFoldError, record_thread_fold, set_operational
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import Attachment, RunnerClient, RunnerError, StreamClosedError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
RECONCILE_S = 2
LEASE_DURATION = timedelta(seconds=30)


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


class Feed:
    """One lease owner's ingestion connection. Browsers never subscribe to this object."""

    def __init__(
        self,
        *,
        session_id: str,
        client: RunnerClient,
        event_logs: EventLogStore,
        ingestion: Ingestion,
        lease: IngestionLease,
    ):
        self.session_id = session_id
        self.client = client
        self.event_logs = event_logs
        self.ingestion = ingestion
        self.lease = lease
        self.task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        attachment: Attachment | None = None
        try:
            async with asyncio.timeout(10):
                attachment = await self.client.attach(self.session_id)
            attached = attachment.attached
            thread_id = await self.event_logs.open(self.lease.sandbox, self.session_id, attached.spec)
            stored = await self.event_logs.last_cursor(thread_id)
            if stored > attached.last_cursor:
                if await self.event_logs.feed_state(thread_id) is None:
                    await self.ingestion.set_attached(thread_id, attached, lease=self.lease)
                await self.ingestion.end_feed(
                    thread_id,
                    lease=self.lease,
                    error="runner log cursor regressed; refusing to merge a different session history",
                )
                return
            if stored:
                attachment.cancel()
                async with asyncio.timeout(10):
                    # Replay the boundary entry too: the same cursor must still identify the
                    # exact archived Event and source even if the runner has no new entries.
                    attachment = await self.client.attach(self.session_id, after_cursor=stored - 1)
            await self.ingestion.set_attached(thread_id, attachment.attached, lease=self.lease)
            try:
                async with contextlib.aclosing(event_batches(attachment.next_entry)) as batches:
                    async for batch in batches:
                        await self.ingestion.record(thread_id, batch, lease=self.lease)
                copied = await self.event_logs.last_cursor(thread_id)
                await self.ingestion.end_feed(
                    thread_id,
                    lease=self.lease,
                    error=(
                        f"runner replay ended at cursor {copied} before promised cursor {attachment.attached.last_cursor}"
                        if copied < attachment.attached.last_cursor
                        else None
                    ),
                )
            except EventReplicationError as error:
                logger.error("invalid runner history for %s/%s", self.lease.sandbox, self.session_id, exc_info=True)
                await self.ingestion.end_feed(thread_id, lease=self.lease, error=str(error), error_cursor=error.cursor)
        except IngestionLeaseLostError:
            logger.info("ingestion lease lost for %s/%s", self.lease.sandbox, self.session_id)
        except grpc.aio.AioRpcError, ConnectionError, SQLAlchemyError, RunnerError, TimeoutError:
            # Reconcile retries from the committed cursor. A transport loss is not session end.
            logger.warning("ingestion interrupted for %s/%s", self.lease.sandbox, self.session_id, exc_info=True)
        finally:
            if attachment is not None:
                attachment.cancel()

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class Ingester:
    """Copies the runner sessions of every running sandbox into the event log: one lease per sandbox
    across app replicas, and one `Feed` per session under it."""

    def __init__(self, *, runners: Runners, event_logs: EventLogStore, ingestion: Ingestion) -> None:
        self._runners = runners
        self._event_logs = event_logs
        self._ingestion = ingestion
        self._feeds: dict[tuple[str, str], Feed] = {}
        self._leases: dict[str, IngestionLease] = {}
        self._changed = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._coordinator: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the ingestion coordinator, or wake it to reconcile now."""
        if self._coordinator is None:
            self._coordinator = asyncio.create_task(self._coordinate(), name="sandbox-ingestion")
        self._changed.set()

    async def _coordinate(self) -> None:
        with self._runners.changes.subscribe(self._changed):
            await self._coordinate_subscribed()

    async def _coordinate_subscribed(self) -> None:
        while True:
            self._changed.clear()
            try:
                await self.reconcile()
            except SQLAlchemyError, grpc.aio.AioRpcError, OSError:
                logger.warning("sandbox ingestion reconciliation failed; will retry", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=RECONCILE_S)

    async def reconcile(self) -> None:
        """Renew ownership of the running sandboxes and discover sessions opened through any replica."""
        async with self._reconcile_lock:
            running = self._runners.running()
            for sandbox in set(self._leases) - running:
                await self._release(sandbox)
            async with asyncio.TaskGroup() as tasks:
                for sandbox in sorted(running):
                    tasks.create_task(self._reconcile_sandbox(sandbox))

    async def _reconcile_sandbox(self, sandbox: str) -> None:
        try:
            async with asyncio.timeout(10):
                lease = self._leases.get(sandbox)
                if lease is not None and not await self._ingestion.renew(lease, LEASE_DURATION):
                    await self._release(sandbox)
                    lease = None
                if lease is None:
                    lease = await self._ingestion.acquire(sandbox, LEASE_DURATION)
                    if lease is None:
                        return
                    self._leases[sandbox] = lease
                try:
                    async with asyncio.timeout(5):
                        client = self._runners.client(sandbox)
                        summaries = await client.list_sessions()
                    for summary in summaries:
                        key = (sandbox, summary.session_id)
                        feed = self._feeds.get(key)
                        if feed is not None and feed.task is not None and not feed.task.done():
                            if feed.client is client:
                                continue
                            await feed.close()
                        thread_id = await self._event_logs.open(sandbox, summary.session_id, summary.spec)
                        snapshot = await self._event_logs.feed_state(thread_id)
                        # A semantic replay failure is durable evidence that this runner's prefix is
                        # unsafe. A new coordinator or app replica must not call set_attached() and
                        # make its failed thread view appear healthy before replaying the same
                        # rejected suffix again. A distinct session is the explicit recovery path.
                        if snapshot is not None and isinstance(snapshot.end, FeedError):
                            continue
                        if (
                            summary.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
                            and snapshot is not None
                            and snapshot.end is not None
                            and await self._event_logs.last_cursor(thread_id) == summary.last_cursor
                        ):
                            continue
                        feed = Feed(
                            session_id=summary.session_id,
                            client=client,
                            event_logs=self._event_logs,
                            ingestion=self._ingestion,
                            lease=lease,
                        )
                        feed.task = asyncio.create_task(feed.run(), name=f"ingest-{sandbox}-{summary.session_id}")
                        self._feeds[key] = feed
                except grpc.aio.AioRpcError, SandboxNotReachableError, SandboxNotFoundError, TimeoutError:
                    logger.warning("sandbox %s ingestion discovery unavailable", sandbox, exc_info=True)
        except SQLAlchemyError, OSError, TimeoutError:
            logger.warning("sandbox %s ingestion reconciliation failed; will retry", sandbox, exc_info=True)

    async def _release(self, sandbox: str) -> None:
        for key in [key for key in self._feeds if key[0] == sandbox]:
            await self._feeds.pop(key).close()
        await self._ingestion.release(self._leases.pop(sandbox))

    async def close(self) -> None:
        if self._coordinator is not None:
            self._coordinator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._coordinator
            self._coordinator = None
        for sandbox in list(self._leases):
            await self._release(sandbox)
