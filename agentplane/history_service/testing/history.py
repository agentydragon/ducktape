"""Scripted Session history in a history database, folded by the History Service's own fold.

A consumer under test reads it only through the History Service (`backend`), as in production.
Tests write a Session's entries and feed state here, as the History Service ingester would, and
each write folds before it returns.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.history_service.fold_shadow import FoldShadow
from agentplane.history_service.summary_changes import notify_summaries
from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.sandbox_service.session_history.database_migrate import RUNNER
from agentplane.sandbox_service.session_history.db import SessionHistory
from agentplane.sandbox_service.session_history.store import Store
from agentplane.thread_fold.recording import set_operational

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf

SANDBOX_NAMESPACE = "test-history-sandbox-namespace"
FOLD_INTERVAL_S = 0.05


def migrate(database_url: str) -> None:
    """Migrate a disposable history database as the Sandbox Service's migration job does."""
    RUNNER.apply(database_url)


class History:
    """One test's history database. `folding` also folds what other writers commit, as the deployed fold does."""

    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = Store(self.engine)
        self._fold = FoldShadow(engine=self.engine, history=self.store, concurrency=4, page_size=1000)
        self._folding = asyncio.Lock()
        self._attachments: dict[UUID, runner_pb2.Attached] = {}

    async def open(self, session_id: UUID, *, sandbox: str) -> None:
        """Register the Session unless a Sandbox Service already did."""
        async with self.sessions() as session:
            if await session.get(SessionHistory, session_id) is not None:
                return
        await self.store.record_session(
            session_id,
            sandbox_namespace=SANDBOX_NAMESPACE,
            sandbox_name=sandbox,
            sandbox_uid=None,
            runner_session_id=str(session_id),
        )

    async def fold(self) -> None:
        """Fold every Session until none is behind, then hand the leases back for the next folder."""
        async with self._folding:
            try:
                while (await self._fold.cycle()).behind:
                    pass
            finally:
                await self._fold.release()

    @asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Hold off this History's folds, so a test can seed the fold's rows itself."""
        async with self._folding:
            yield

    @asynccontextmanager
    async def folding(self) -> AsyncIterator[None]:
        async def run() -> None:
            while True:
                await self.fold()
                await asyncio.sleep(FOLD_INTERVAL_S)

        task = asyncio.create_task(run(), name="test-history-fold")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def record(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> None:
        cursor = await self.store.append(session_id, entries)
        pending = self._attachments.get(session_id)
        if pending is not None and pending.last_cursor <= cursor:
            await self.store.record_feed_state(session_id, history_pb2.SessionFeedState(attached=pending))
            del self._attachments[session_id]
        await self.fold()

    async def set_attached(self, session_id: UUID, attached: runner_pb2.Attached) -> None:
        snapshot = runner_pb2.Attached()
        snapshot.CopyFrom(attached)
        snapshot.session_id = str(session_id)
        async with self.sessions() as session:
            row = await session.get(SessionHistory, session_id)
        if row is None:
            raise ValueError("open the Session first")
        if row.last_cursor < snapshot.last_cursor:
            # A fixture may describe a runner snapshot before seeding its entries; the store takes
            # it once the entries cover it.
            self._attachments[session_id] = snapshot
            return
        await self.store.record_feed_state(session_id, history_pb2.SessionFeedState(attached=snapshot))
        await self.fold()

    async def end_feed(self, session_id: UUID, *, error: str | None, error_cursor: int | None = None) -> None:
        if error is not None:
            # A folded failure state for UI tests, not a fake archive failure.
            async with self.sessions.begin() as session:
                await set_operational(session, session_id, status="failed", error=error, error_cursor=error_cursor)
                await notify_summaries(session)
            return
        async with self.sessions() as session:
            row = await session.get(SessionHistory, session_id)
        if row is None or row.feed_state is None:
            raise ValueError("seed an attachment before terminal state")
        terminal = history_pb2.SessionFeedState.FromString(row.feed_state)
        terminal.ended = True
        await self.store.record_feed_state(session_id, terminal)
        await self.fold()

    async def close(self) -> None:
        await self.engine.dispose()
