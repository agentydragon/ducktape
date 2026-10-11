"""The shadow fold over a migrated history database, read from its own raw log."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.history_service.fold_shadow import FOLD_EPOCH, FoldBacklog, FoldShadow
from agentplane.history_service.storage.history_changes import HistoryChanges
from agentplane.history_service.storage.store import Store
from agentplane.proto import event_log_pb2, event_pb2
from agentplane.sandbox_service.testing.delta_spans import claude_text
from agentplane.thread_fold.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadHistorySummary,
    ThreadNativeLink,
    ThreadPayloadChunk,
)
from agentplane.thread_fold.projector import PAGE_SIZE
from agentplane.thread_fold.views import EntityKind

# gazelle:include_dep //agentplane/history_service/storage:conftest

pytest_plugins = ("agentplane.history_service.storage.conftest",)


def entry(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 10, 10, 12, 0, tzinfo=UTC) + timedelta(seconds=cursor))
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor),
        event=event_pb2.Event(at=at, **observation),  # type: ignore[arg-type]
    )


def turn(start: int) -> list[event_log_pb2.EventEntry]:
    """One completed turn whose assistant text spans more than a page of deltas."""
    deltas = PAGE_SIZE + 2
    return [
        entry(start, turn_started=event_pb2.TurnStarted(turn_id="test-turn")),
        entry(
            start + 1, item_started=event_pb2.ItemStarted(item_id="test-reply", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        ),
        *(
            entry(start + 2 + index, text_delta=event_pb2.TextDelta(item_id="test-reply", text="x"))
            for index in range(deltas)
        ),
        entry(start + 2 + deltas, item_completed=event_pb2.ItemCompleted(item_id="test-reply", text="x" * deltas)),
        entry(
            start + 3 + deltas,
            turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=event_pb2.TURN_STATUS_COMPLETED),
        ),
    ]


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)


async def retained_session(history: Store, entries: list[event_log_pb2.EventEntry]) -> UUID:
    session_id = uuid4()
    # A deleted Sandbox's Session is folded like any other.
    await history.record_session(
        session_id,
        sandbox_namespace="test-namespace",
        sandbox_name="test-deleted-sandbox",
        sandbox_uid=None,
        runner_session_id=f"test-runner-{session_id}",
    )
    await history.append(session_id, entries)
    return session_id


async def test_folds_retained_sessions_a_page_per_cycle_until_caught_up(engine: AsyncEngine, history: Store) -> None:
    entries = [entry(1, harness_started=event_pb2.HarnessStarted(pid=7)), *turn(2)]
    folded = await retained_session(history, entries)
    empty = await retained_session(history, [])
    shadow = FoldShadow(engine=engine, history=history, concurrency=2)

    assert await shadow.cycle() == FoldBacklog(sessions=2, behind=1, events_behind=len(entries) - PAGE_SIZE)
    assert await shadow.cycle() == FoldBacklog(sessions=2, behind=0, events_behind=0)

    async with async_sessionmaker(engine)() as session:
        checkpoint = await session.get(ThreadCheckpoint, folded)
        assert checkpoint is not None
        assert (checkpoint.projection_epoch, checkpoint.through_cursor) == (FOLD_EPOCH, len(entries))
        reply = await session.get(ThreadEntity, (folded, FOLD_EPOCH, EntityKind.ITEM, "test-reply"))
        assert reply is not None
        assert not reply.pending
        summary = await session.get(ThreadHistorySummary, folded)
        assert summary is not None
        assert summary.last_turn_status == event_pb2.TURN_STATUS_COMPLETED
        assert await session.get(ThreadHistorySummary, empty) is not None
        assert await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == empty)) is None


async def test_run_folds_without_waiting_out_the_interval_while_behind(engine: AsyncEngine, history: Store) -> None:
    entries = turn(1)
    session_id = await retained_session(history, entries)
    shadow = FoldShadow(engine=engine, history=history, concurrency=1)
    changes = HistoryChanges(engine.url)
    sessions = async_sessionmaker(engine)

    async def caught_up(cursor: int) -> None:
        while True:
            async with sessions() as session:
                through = await session.scalar(
                    select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == session_id)
                )
            if through == cursor:
                return
            await asyncio.sleep(0.05)

    async with changes.listener.listen(), asyncio.TaskGroup() as tasks:
        # More than a page behind: an interval wait between pages would outlast the timeout.
        running = tasks.create_task(shadow.run(interval_s=3600, changes=changes))
        async with asyncio.timeout(30):
            await caught_up(len(entries))
            await history.append(session_id, [entry(len(entries) + 1, harness_started=event_pb2.HarnessStarted())])
            await caught_up(len(entries) + 1)
        running.cancel()


async def test_replicas_split_sessions_by_lease(engine: AsyncEngine, history: Store) -> None:
    session_id = await retained_session(history, turn(1))
    first = FoldShadow(engine=engine, history=history, concurrency=1)
    second = FoldShadow(engine=engine, history=history, concurrency=1)

    assert (await first.cycle()).behind == 1
    # The first replica's lease is live, so the second leaves the Session alone.
    assert (await second.cycle()).behind == 0
    async with async_sessionmaker(engine)() as session:
        assert (
            await session.scalar(
                select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == session_id)
            )
            == PAGE_SIZE
        )


async def folded_rows(session: AsyncSession, session_id: UUID) -> list[tuple[object, ...]]:
    """Each entity with its body text. A body's generation is the cursor that last wrote it, which
    a settled item's chunks no longer are."""
    rows: list[tuple[object, ...]] = []
    for entity in await session.scalars(
        select(ThreadEntity)
        .where(ThreadEntity.thread_id == session_id, ThreadEntity.projection_epoch == FOLD_EPOCH)
        .order_by(ThreadEntity.entity_index)
    ):
        body = None
        if entity.text_ref is not None:
            chunks = await session.scalars(
                select(ThreadPayloadChunk.text)
                .where(
                    ThreadPayloadChunk.thread_id == session_id,
                    ThreadPayloadChunk.projection_epoch == FOLD_EPOCH,
                    ThreadPayloadChunk.owner_id == entity.text_ref["owner_id"],
                    ThreadPayloadChunk.field == entity.text_ref["field"],
                    ThreadPayloadChunk.generation == int(str(entity.text_ref["generation"])),
                )
                .order_by(ThreadPayloadChunk.chunk_index)
            )
            body = "".join(chunks)
        rows.append(
            (
                entity.entity_kind,
                entity.entity_id,
                entity.cursor,
                entity.revision_cursor,
                entity.pending,
                entity.state,
                body,
            )
        )
    return rows


async def observed_cursors(session: AsyncSession, session_id: UUID) -> tuple[set[int], set[int]]:
    """The derived Event cursors a fold cites as evidence, and the native frame cursors it links."""
    evidence = await session.scalars(
        select(ThreadEvidence.observation_cursor).where(ThreadEvidence.thread_id == session_id)
    )
    native = await session.scalars(
        select(ThreadNativeLink.source_sequence).where(ThreadNativeLink.thread_id == session_id)
    )
    return set(evidence), set(native)


async def test_settled_history_folds_to_the_rows_of_the_full_log(engine: AsyncEngine, history: Store) -> None:
    # 1-2 start, 3-6 two chunk pairs, 7-8 completion.
    span = claude_text(["Hel", "lo"]).entries
    full = await retained_session(history, span)
    settled = await retained_session(Store(engine, settle_deltas=True), span)
    shadow = FoldShadow(engine=engine, history=history, concurrency=2)

    assert await shadow.cycle() == FoldBacklog(sessions=2, behind=0, events_behind=0)

    async with async_sessionmaker(engine)() as session:
        assert await folded_rows(session, settled) == await folded_rows(session, full)
        for session_id in (full, settled):
            assert await session.scalar(
                select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == session_id)
            ) == len(span)
        evidence, native = await observed_cursors(session, settled)
        assert not (evidence | native) & set(range(3, 7))


async def test_settlement_after_a_live_fold_drops_the_chunks_evidence(engine: AsyncEngine) -> None:
    settling = Store(engine, settle_deltas=True)
    span = claude_text(["Hel", "lo"]).entries
    session_id = await retained_session(settling, span[:6])
    shadow = FoldShadow(engine=engine, history=settling, concurrency=1)

    await shadow.cycle()
    async with async_sessionmaker(engine)() as session:
        evidence, native = await observed_cursors(session, session_id)
        assert ({4, 6} <= evidence, {3, 5} <= native) == (True, True)

    await settling.append(session_id, span[6:])
    await shadow.cycle()
    async with async_sessionmaker(engine)() as session:
        evidence, native = await observed_cursors(session, session_id)
        assert not (evidence | native) & set(range(3, 7))
        assert 8 in evidence
        reply = await session.get(ThreadEntity, (session_id, FOLD_EPOCH, EntityKind.ITEM, "msg_test#0"))
        assert reply is not None
        assert not reply.pending


if __name__ == "__main__":
    pytest_bazel.main()
