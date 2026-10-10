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

from agentplane.app.database_updates import Channel, notify
from agentplane.app.threads.events import event_log, ingestion_lease
from agentplane.app.threads.events.event_log import EventLogStore, EventReplicationError, FeedError, RunnerSession
from agentplane.app.threads.events.ingestion_lease import IngestionLease, IngestionLeaseLostError
from agentplane.app.threads.history_projector import HistoryProjector
from agentplane.app.threads.sessions import SandboxNotReachableError, SandboxSessions
from agentplane.app.threads.view import fold
from agentplane.app.threads.view.recording import ThreadFoldError, record_thread_fold, set_operational
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service.client import Attachment, ReconnectRequiredError, Runner, ServiceError
from agentplane.sandbox_service.models import SandboxNotFoundError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
RECONCILE_S = 2
RECONNECT_WARNING_S = 30
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
                except ReconnectRequiredError:
                    yield batch
                    raise
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
            except (fold.ObservationNotUnderstoodError, fold.FoldContractError) as error:
                raise ThreadFoldError(
                    f"thread fold failed at cursor {error.cursor}: {error}", cursor=error.cursor
                ) from error
            except ValueError as error:
                raise ThreadFoldError(f"thread fold failed: {error}") from error
            # The maximum stored cursor is the checkpoint: the fenced transaction admits
            # only a contiguous suffix, so there is no separately mutable progress counter.
            await event_log.advance_feed(session, thread_id, inserted)
            await notify(session, Channel.THREADS)

    async def set_attached(self, thread_id: UUID, attached: protocol_pb2.Attached, *, lease: IngestionLease) -> None:
        async with self._sessions.begin() as session:
            await ingestion_lease.fence(session, lease, thread_id)
            await event_log.set_attached(session, thread_id, attached)
            await set_operational(session, thread_id, status="active", error=None)
            await notify(session, Channel.THREADS)

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
            await notify(session, Channel.THREADS)


class Feed:
    """One lease owner's ingestion connection. Browsers never subscribe to this object."""

    def __init__(
        self, *, session_id: str, client: Runner, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
    ):
        self.session_id = session_id
        self.client = client
        self.event_logs = event_logs
        self.ingestion = ingestion
        self.lease = lease
        self.task: asyncio.Task[None] | None = None
        self._connected_at: float | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        retry_since: float | None = None
        last_warning = 0.0
        while True:
            self._connected_at = None
            try:
                await self._copy()
                return
            except ReconnectRequiredError:
                logger.debug("renewing ingestion for %s/%s", self.lease.sandbox, self.session_id)
                retry_since = None
                continue
            except IngestionLeaseLostError:
                logger.info("ingestion lease lost for %s/%s", self.lease.sandbox, self.session_id)
                return
            except (grpc.aio.AioRpcError, ConnectionError, SQLAlchemyError, RunnerError, TimeoutError) as error:
                now = loop.time()
                # Keep retry state across attempts, but not across a healthy long-lived follow.
                if retry_since is None or (
                    self._connected_at is not None and now - self._connected_at >= RECONNECT_WARNING_S
                ):
                    retry_since = now
                denied = isinstance(error, ServiceError) and error.code in (
                    grpc.StatusCode.UNAUTHENTICATED,
                    grpc.StatusCode.PERMISSION_DENIED,
                )
                if (denied or now - retry_since >= RECONNECT_WARNING_S) and (
                    not last_warning or now - last_warning >= RECONNECT_WARNING_S
                ):
                    logger.warning(
                        "ingestion reconnect unsuccessful for %s/%s; retrying for %.1fs (%s)",
                        self.lease.sandbox,
                        self.session_id,
                        now - retry_since,
                        type(error).__name__,
                        exc_info=True,
                    )
                    last_warning = now
                else:
                    logger.debug(
                        "reconnecting ingestion for %s/%s (%s)",
                        self.lease.sandbox,
                        self.session_id,
                        type(error).__name__,
                    )
                # Also handles a lost renewal marker/bare transport EOF; never end the feed.
                await asyncio.sleep(RECONCILE_S)

    async def _copy(self) -> None:
        attachment: Attachment | None = None
        try:
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
                # Replay the boundary entry too: the same cursor must still identify the
                # exact archived Event and source even if the runner has no new entries.
                attachment = await self.client.attach(self.session_id, after_cursor=stored - 1)
            await self.ingestion.set_attached(thread_id, attachment.attached, lease=self.lease)
            self._connected_at = asyncio.get_running_loop().time()
            logger.debug("ingestion attached for %s/%s", self.lease.sandbox, self.session_id)
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

    def __init__(
        self,
        *,
        runners: SandboxSessions,
        event_logs: EventLogStore,
        ingestion: Ingestion,
        history_projector: HistoryProjector | None = None,
    ) -> None:
        self._runners = runners
        self._history_projector = history_projector
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
            running = set(self._runners.running())
            fenced = await self._event_logs.fenced_sessions()
            if self._history_projector is not None:
                running.update(locator.sandbox for locator in fenced.values())
            for sandbox in set(self._leases) - running:
                await self._release(sandbox)
            async with asyncio.TaskGroup() as tasks:
                for sandbox in sorted(running):
                    tasks.create_task(self._reconcile_sandbox(sandbox, fenced))

    async def _reconcile_sandbox(self, sandbox: str, fenced: dict[UUID, RunnerSession]) -> None:
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
                # Stop this replica's obsolete feed before projection. The DB fence
                # also rejects delayed writes from old replicas and renewed leases.
                selected = {thread: locator for thread, locator in fenced.items() if locator.sandbox == sandbox}
                for locator in selected.values():
                    feed = self._feeds.pop((sandbox, locator.session_id), None)
                    if feed is not None:
                        await feed.close()
                if self._history_projector is not None:
                    async with asyncio.TaskGroup() as tasks:
                        for thread_id in selected:
                            tasks.create_task(self._project_history(thread_id, lease))
                if sandbox not in self._runners.running():
                    return
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
                        try:
                            thread_id = await self._event_logs.open(sandbox, summary.session_id, summary.spec)
                        except EventReplicationError:
                            logger.warning(
                                "session %s/%s requires explicit history reconciliation", sandbox, summary.session_id
                            )
                            continue
                        if thread_id in fenced or await self._event_logs.is_raw_ingestion_fenced(thread_id):
                            continue
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
                except (
                    grpc.aio.AioRpcError,
                    ConnectionError,
                    SandboxNotReachableError,
                    SandboxNotFoundError,
                    TimeoutError,
                ):
                    logger.warning("sandbox %s ingestion discovery unavailable", sandbox, exc_info=True)
        except SQLAlchemyError, OSError, TimeoutError:
            logger.warning("sandbox %s ingestion reconciliation failed; will retry", sandbox, exc_info=True)

    async def _project_history(self, thread_id: UUID, lease: IngestionLease) -> None:
        assert self._history_projector is not None
        try:
            progress = await self._history_projector.project_batch(thread_id, lease=lease)
            logger.debug(
                "service projection %s: through=%s service=%s",
                thread_id,
                progress.through_cursor,
                progress.service_cursor,
            )
        except (
            EventReplicationError,
            IngestionLeaseLostError,
            SQLAlchemyError,
            grpc.aio.AioRpcError,
            ConnectionError,
            ValueError,
        ):
            # One failed UI fold must not stop other Threads or the service archive.
            logger.warning("service projection stalled for %s; checkpoint retained", thread_id, exc_info=True)

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
