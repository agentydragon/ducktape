"""Copy runner journals into the history tables as an ordinary Sandbox Service subscriber.

`WatchSessions` names every Session and its current incarnation; each is followed with
`FollowSession` from the cursor this service committed (creating its history row first), and every
batch is one `Store.append`. The runner journal is the buffer: after an outage or restart,
following resumes from the committed cursor, and a retention hold on each followed Session keeps
its Sandbox from being deleted until the copy reaches the seal. A per-Session claim keeps replicas
from duplicating work, while `Store.append` (exact replays accepted, gaps and conflicts refused)
keeps a log correct without it.
"""

import asyncio
import enum
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import grpc
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.protocol_pb2 import SessionFeedState
from agentplane.history_service.storage.store import HistoryConflictError, Store
from agentplane.proto import event_log_pb2
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service.client import (
    Attachment,
    ReconnectRequiredError,
    Runner,
    SandboxServiceClient,
    SealedError,
    ServiceError,
)
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, Sealed, SessionChange

# Generated stubs require the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# This service's name on the retention holds it places.
HOLDER = "history"


@asynccontextmanager
async def claim(engine: AsyncEngine, session_id: UUID) -> AsyncIterator[bool]:
    """Hold this Session's log for one writer across replicas; yields whether this caller got it.

    A session-level advisory lock on a dedicated connection: PostgreSQL releases it when the
    connection closes, so a replica that dies hands the Session over without a lease to expire.
    """
    key = func.hashtextextended(f"agentplane-history-ingest/{session_id}", 0)
    async with engine.connect() as connection:
        acquired = bool(await connection.scalar(select(func.pg_try_advisory_lock(key))))
        await connection.commit()
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            try:
                await connection.scalar(select(func.pg_advisory_unlock(key)))
                await connection.commit()
            except BaseException:
                # Never return a connection that may still hold the lock to the pool.
                await connection.invalidate()
                raise


class Next(enum.Enum):
    """When a Session's next follow starts."""

    NOW = enum.auto()  # planned renewal
    LATER = enum.auto()  # ended, failed, or claimed elsewhere; the journal may still grow
    ON_CHANGE = enum.auto()  # sealed or its incarnation is gone; only a new SessionChange revives it


class HistoryIngester:
    """Follows at most `concurrency` Sessions at once; a follow lasts until the Sandbox Service's
    planned renewal, so waiting Sessions rotate in within one follow lease."""

    def __init__(
        self,
        store: Store,
        engine: AsyncEngine,
        sandboxes: SandboxServiceClient,
        *,
        concurrency: int,
        retry_interval_s: float,
        batch_size: int = 128,
        fresh_channel_after: int = 4,
    ) -> None:
        if concurrency < 1 or retry_interval_s <= 0 or batch_size < 1 or fresh_channel_after < 1:
            raise ValueError("concurrency, retry interval, batch size and fresh-channel threshold must be positive")
        self.store = store
        self.engine = engine
        self.sandboxes = sandboxes
        self.concurrency = concurrency
        self.retry_interval_s = retry_interval_s
        self.batch_size = batch_size
        self.fresh_channel_after = fresh_channel_after
        self._latest: dict[UUID, SessionChange] = {}
        # Sessions queued, being followed, or waiting to retry; each is in the queue at most once.
        self._scheduled: set[UUID] = set()
        self._ready: asyncio.Queue[UUID] = asyncio.Queue()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            for _ in range(self.concurrency):
                tasks.create_task(self._work())
            await self._watch()

    async def _watch(self) -> None:
        # The position lives in memory: a restarted replica reads every Session's latest record
        # again, and each resumes from its committed cursor.
        position = 0
        failures = 0
        while True:
            try:
                async for change in self.sandboxes.watch_sessions(after_position=position):
                    failures = 0
                    self._observe(change)
                    position = change.position
                failures = 0
            except ConnectionError, TimeoutError, RunnerError:
                failures += 1
                # The chained gRPC error carries the status details the client keeps out of its message.
                logger.warning("Session feed failed after position %d; reconnecting", position, exc_info=True)
                if failures % self.fresh_channel_after == 0:
                    # A channel once stayed UNAVAILABLE for half an hour against a healthy Sandbox Service
                    # that a fresh process reached at once. Follows on the old channel fail and retry.
                    logger.warning("Session feed failed %d times in a row; dialing a fresh channel", failures)
                    await self.sandboxes.close()
                await asyncio.sleep(self.retry_interval_s)

    def _observe(self, change: SessionChange) -> None:
        session_id = UUID(change.session_id)
        self._latest[session_id] = change
        if session_id not in self._scheduled:
            self._scheduled.add(session_id)
            self._ready.put_nowait(session_id)

    async def _work(self) -> None:
        while True:
            session_id = await self._ready.get()
            change = self._latest[session_id]
            try:
                after = await self._follow(change)
            except HistoryConflictError:
                logger.exception("Session %s history conflicts with the stored prefix; not copied", session_id)
                after = Next.LATER
            except ServiceError as error:
                if error.code == grpc.StatusCode.NOT_FOUND:
                    after = Next.ON_CHANGE
                else:
                    logger.warning("Session %s follow failed (%s)", session_id, error.code.name)
                    after = Next.LATER
            except (ConnectionError, TimeoutError, RunnerError, SQLAlchemyError) as error:
                logger.warning("Session %s follow failed (%r)", session_id, error)
                after = Next.LATER
            if self._latest[session_id] is not change:
                after = Next.NOW  # a newer record arrived while following
            if after is Next.NOW:
                self._ready.put_nowait(session_id)
            elif after is Next.LATER:
                asyncio.get_running_loop().call_later(self.retry_interval_s, self._ready.put_nowait, session_id)
            else:
                self._scheduled.discard(session_id)

    async def _follow(self, change: SessionChange) -> Next:
        """One follow of one Session under its claim, from the committed cursor."""
        if not change.sandbox_uid or not change.HasField("owner"):
            return Next.ON_CHANGE  # imported without an incarnation, or its Sandbox is gone
        session_id = UUID(change.session_id)
        async with claim(self.engine, session_id) as claimed:
            if not claimed:
                return Next.LATER
            # The owner is the Sandbox's ServiceAccount, in the Sandbox's own namespace.
            await self.store.record_session(
                session_id,
                sandbox_namespace=change.owner.namespace,
                sandbox_name=change.sandbox,
                sandbox_uid=UUID(change.sandbox_uid),
                runner_session_id=None,
            )
            destination = SandboxDestination(owner=change.owner, sandbox=change.sandbox, sandbox_uid=change.sandbox_uid)
            runner = self.sandboxes.runner(destination)
            await self._hold(runner, change.session_id)
            cursor, _ = await self.store.read(session_id, limit=1)
            attachment = await runner.attach(change.session_id, after_cursor=cursor)
            try:
                return await self._copy(session_id, runner, attachment, cursor)
            finally:
                attachment.cancel()

    async def _hold(self, runner: Runner, session_id: str) -> None:
        """Keep the Sandbox from being deleted before this service has the Session through its seal.

        The hold stays after the seal is confirmed: it goes with the Sandbox incarnation it holds.
        """
        try:
            await runner.place_hold(session_id, holder=HOLDER)
        except ServiceError as error:
            if error.code != grpc.StatusCode.INVALID_ARGUMENT:
                raise
            # The Sandbox is sealed and this Session has no seal: nothing can satisfy a hold, but
            # the runner may still serve what it journaled.
            logger.warning("Session %s cannot be held in its sealed Sandbox; copying without a hold", session_id)

    async def _copy(self, session_id: UUID, runner: Runner, attachment: Attachment, cursor: int) -> Next:
        through = attachment.attached.last_cursor
        if through < cursor:
            raise HistoryConflictError(f"runner journal ends at {through}, below committed cursor {cursor}")
        batch: list[event_log_pb2.EventEntry] = []

        async def commit() -> None:
            if batch:
                await self.store.append(session_id, batch)
                batch.clear()

        if cursor == through:
            await self.store.record_feed_state(session_id, SessionFeedState(attached=attachment.attached))
        while True:
            try:
                entry = await attachment.next_entry()
            except ReconnectRequiredError:
                await commit()
                return Next.NOW
            except SealedError as sealed:
                await commit()
                if sealed.cursor != cursor:
                    raise HistoryConflictError(f"seal at {sealed.cursor} does not follow entry {cursor}") from sealed
                await self.store.record_feed_state(
                    session_id, SessionFeedState(attached=attachment.attached, sealed=Sealed(cursor=cursor))
                )
                # Only a confirmation at the seal changes the deletion gate, so earlier batches skip
                # the Kubernetes write.
                await runner.confirm_hold(str(session_id), holder=HOLDER, through_cursor=cursor)
                return Next.ON_CHANGE
            except StreamClosedError:
                await commit()
                # EOF is evidence only at the attach-time snapshot's cursor.
                if cursor == through:
                    await self.store.record_feed_state(
                        session_id, SessionFeedState(attached=attachment.attached, ended=True)
                    )
                return Next.LATER
            if entry.cursor != cursor + 1:
                raise HistoryConflictError(f"follow expected cursor {cursor + 1}, got {entry.cursor}")
            batch.append(entry)
            cursor = entry.cursor
            # Replay commits in batches; live entries commit as they arrive.
            if cursor >= through or len(batch) >= self.batch_size:
                await commit()
            if cursor == through:
                await self.store.record_feed_state(session_id, SessionFeedState(attached=attachment.attached))
