"""Scripted Session history in a history database, folded by the History Service's own fold.

A consumer under test reads it only through the History Service (`backend`), as in production.
Tests write a Session's entries and feed state here, as the History ingress would, and
each write folds before it returns.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence, Set
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC
from uuid import UUID

from more_itertools import one
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agentplane.history_service.channels import Channel
from agentplane.history_service.fold import fold
from agentplane.history_service.fold.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadHistorySummary,
    ThreadPayloadChunk,
    ThreadPayloadManifest,
)
from agentplane.history_service.fold.recording import set_operational
from agentplane.history_service.fold.rows import ordered_entity_rows
from agentplane.history_service.fold.views import EntityKind
from agentplane.history_service.fold_runner import FoldRunner
from agentplane.history_service.storage.database_migrate import RUNNER
from agentplane.history_service.storage.db import SessionHistory
from agentplane.history_service.storage.store import Store
from agentplane.history_service.testing.sessions import record_follower_session
from agentplane.postgres.wakeups import notify
from agentplane.proto import event_log_pb2, history_service_pb2, runner_pb2
from util.testing.postgres import PostgresDatabaseTemplate, migrated_database_template

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf

SANDBOX_NAMESPACE = "test-history-sandbox-namespace"
FOLD_INTERVAL_S = 0.05


def migrate(database_url: str) -> None:
    """Migrate a disposable history database as the Sandbox Service's migration job does."""
    RUNNER.apply(database_url)


@contextmanager
def migrated_template(admin_url: str, template_name: str) -> Iterator[PostgresDatabaseTemplate]:
    """A history database template migrated once, for tests that clone many history databases."""
    with migrated_database_template(admin_url, template_name, RUNNER) as template:
        yield template


class History:
    """One test's history database. `folding` also folds what other writers commit, as the deployed fold does."""

    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = Store(self.engine)
        self._fold = FoldRunner(engine=self.engine, history=self.store, concurrency=4, page_size=1000)
        self._folding = asyncio.Lock()
        self._attachments: dict[UUID, runner_pb2.Attached] = {}

    async def open(self, session_id: UUID, *, sandbox: str) -> None:
        """Register the Session unless a Sandbox Service already did."""
        async with self.sessions() as session:
            if await session.get(SessionHistory, session_id) is not None:
                return
        await record_follower_session(
            self.engine,
            session_id,
            sandbox_namespace=SANDBOX_NAMESPACE,
            sandbox_name=sandbox,
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
            await self.store.record_feed_state(session_id, history_service_pb2.SessionFeedState(attached=pending))
            del self._attachments[session_id]
        await self.fold()

    async def seed_folded(
        self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry], *, prior_items: Set[str]
    ) -> None:
        """Fold the stored entries past the checkpoint with the pure fold and bulk-insert the rows.

        Lets a test seed a large folded prefix without folding it batch by batch; hold `paused()` so
        no folder races it. `prior_items` are items the fold treats as already materialized.
        """
        async with self.sessions.begin() as session:
            checkpoint = await session.get(ThreadCheckpoint, session_id)
            if checkpoint is None:
                raise ValueError("fold the Session's first entries before seeding the rest")
            epoch = checkpoint.projection_epoch
            through = checkpoint.through_cursor
            result = fold.advance(
                fold.ViewState(fold.Position(checkpoint.source_id, epoch, through_cursor=through)),
                fold.EventBatch(checkpoint.source_id, through, tuple(entries[through:]), ()),
                fold.PriorEntities(items=dict.fromkeys(prior_items), commands={}, bodies={}),
            )
            if result.state.position.through_cursor != len(entries):
                raise ValueError("the entries must be the Session's whole stored prefix")
            next_index = await session.scalar(
                select(func.max(ThreadEntity.entity_index)).where(
                    ThreadEntity.thread_id == session_id, ThreadEntity.projection_epoch == epoch
                )
            )
            next_index = 0 if next_index is None else next_index + 1
            appends = [write for write in result.payload_writes if isinstance(write, fold.AppendPayload)]
            if len(appends) != len(result.payload_writes) or any(write.base is not None for write in appends):
                raise ValueError("a seeded prefix only creates payloads")
            projected = ordered_entity_rows(
                session_id, result, {write.reference: int(bool(write.text)) for write in appends}
            )
            items = [row for row in projected if row["entity_kind"] == EntityKind.ITEM]
            view_state_row = one(row for row in projected if row["entity_kind"] == EntityKind.VIEW_STATE)
            for index, row in enumerate(items):
                row["entity_index"] = next_index + index
            if items:
                await session.execute(insert(ThreadEntity), items)
            view_state = await session.get(ThreadEntity, (session_id, epoch, EntityKind.VIEW_STATE, "current"))
            if view_state is None:
                raise ValueError("the Session has no folded view state")
            view_state.cursor = result.state.position.through_cursor
            view_state.revision_cursor = result.state.position.through_cursor
            state = view_state_row["state"]
            if not isinstance(state, dict):
                raise TypeError(f"{state=} is not a view state document")
            view_state.state = state
            checkpoint.through_cursor = result.state.position.through_cursor
            if appends:
                await session.execute(
                    insert(ThreadPayloadManifest),
                    [
                        {
                            "thread_id": session_id,
                            **_payload_key(write.reference),
                            "revision_cursor": write.reference.revision_cursor,
                            "chunk_count": int(bool(write.text)),
                            "content_bytes": len(write.text.encode()),
                        }
                        for write in appends
                    ],
                )
            chunks = [
                {"thread_id": session_id, **_payload_key(write.reference), "chunk_index": 0, "text": write.text}
                for write in appends
                if write.text
            ]
            if chunks:
                await session.execute(insert(ThreadPayloadChunk), chunks)
            evidence = [
                {
                    "thread_id": session_id,
                    "projection_epoch": value.projection_epoch,
                    "entity_cursor": value.entity_cursor,
                    "observation_cursor": value.observation_cursor,
                }
                for value in result.evidence_upserts
            ]
            if evidence:
                await session.execute(insert(ThreadEvidence), evidence)
            summary = await session.get(ThreadHistorySummary, session_id)
            if summary is None:
                raise ValueError("the Session has no folded summary")
            summary.first_event_at = entries[0].event.at.ToDatetime(tzinfo=UTC)
            summary.last_event_at = entries[-1].event.at.ToDatetime(tzinfo=UTC)

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
        await self.store.record_feed_state(session_id, history_service_pb2.SessionFeedState(attached=snapshot))
        await self.fold()

    async def end_feed(self, session_id: UUID, *, error: str | None, error_cursor: int | None = None) -> None:
        if error is not None:
            # A folded failure state for UI tests, not a fake archive failure.
            async with self.sessions.begin() as session:
                await set_operational(session, session_id, status="failed", error=error, error_cursor=error_cursor)
                await notify(session, Channel.SUMMARIES)
            return
        async with self.sessions() as session:
            row = await session.get(SessionHistory, session_id)
        if row is None or row.feed_state is None:
            raise ValueError("seed an attachment before terminal state")
        terminal = history_service_pb2.SessionFeedState.FromString(row.feed_state)
        terminal.ended = True
        await self.store.record_feed_state(session_id, terminal)
        await self.fold()

    async def close(self) -> None:
        await self.engine.dispose()


def _payload_key(reference: fold.PayloadRef) -> dict[str, object]:
    return {
        "projection_epoch": reference.projection_epoch,
        "owner_cursor": reference.owner_cursor,
        "owner_id": reference.owner_id,
        "field": reference.field,
        "generation": reference.generation,
    }
