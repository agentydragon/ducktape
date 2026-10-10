"""The shadow fold over a migrated history database, read from its own raw log."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service.fold_shadow import FOLD_EPOCH, PAGE_SIZE, FoldBacklog, FoldShadow
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service.session_history.store import Store
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadEntity, ThreadHistorySummary
from agentplane.thread_fold.views import EntityKind

# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)


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
    await history.open(
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


if __name__ == "__main__":
    pytest_bazel.main()
