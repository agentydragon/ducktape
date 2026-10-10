"""Operator metadata and Thread views use current production projection metadata."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.testing.retained_history import seed_retained_session
from agentplane.app.threads.models import Event, ThreadCheckpoint, ThreadHistorySummary
from agentplane.app.threads.store import ThreadStore
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf


async def seed_projection(engine: AsyncEngine, *, sandbox: str, locator: str, cursor: int) -> UUID:
    thread = await seed_retained_session(engine, sandbox=sandbox, locator=locator)
    async with async_sessionmaker(engine).begin() as session:
        session.add(
            ThreadCheckpoint(thread_id=thread, source_id="source", projection_epoch="epoch", through_cursor=cursor)
        )
        session.add(
            ThreadHistorySummary(
                thread_id=thread,
                last_event_at=datetime(2026, 1, 1, tzinfo=UTC),
                attached=MessageToDict(
                    protocol_pb2.Attached(
                        last_cursor=cursor, harness_state=protocol_pb2.HARNESS_STATE_RUNNING, active_turn_id="turn"
                    )
                ),
            )
        )
    return thread


async def test_thread_views_and_operator_edits_need_no_raw_events(engine: AsyncEngine) -> None:
    thread = await seed_projection(engine, sandbox="one", locator="s-retained", cursor=7)
    other = await seed_projection(engine, sandbox="two", locator="s-other", cursor=3)
    store = ThreadStore(engine)
    view = await store.get_thread(thread)
    assert view is not None
    assert view.last_cursor == 7
    assert view.session_id == "s-retained"
    assert view.harness_state == "HARNESS_STATE_RUNNING"
    assert view.active_turn_id == "turn"
    assert view.feed_status == "active"
    assert [v.id for v in await store.list_threads(sandbox="one")] == [thread]
    assert [v.id for v in await store.list_threads(session_id="s-other")] == [other]
    renamed = await store.rename(thread, "retained")
    assert renamed.name == "retained"
    assert renamed.last_cursor == 7
    assert (await store.rename(thread, None)).name is None
    assert (await store.archive(thread)).archived
    assert [v.id for v in await store.list_threads()] == [other]
    assert {v.id for v in await store.list_threads(include_archived=True)} == {thread, other}
    assert not (await store.unarchive(thread)).archived
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(Event.cursor).limit(1)) is None


@pytest.mark.parametrize(
    "status", [None, event_pb2.TURN_STATUS_COMPLETED, event_pb2.TURN_STATUS_FAILED, event_pb2.TURN_STATUS_INTERRUPTED]
)
async def test_thread_turn_status_comes_from_projection_summary(engine: AsyncEngine, status: int | None) -> None:
    thread = await seed_projection(engine, sandbox="one", locator="s-retained", cursor=7)
    async with async_sessionmaker(engine).begin() as session:
        summary = await session.get(ThreadHistorySummary, thread)
        assert summary is not None
        summary.last_turn_status = status
        summary.end = {}
    store = ThreadStore(engine)
    view = await store.get_thread(thread)
    assert view is not None
    assert view.last_turn_status == (None if status is None else event_pb2.TurnStatus.Name(status))
    assert view.feed_status == "ended"
    assert await store.list_threads() == [view]


async def test_missing_projection_metadata_does_not_read_retained_raw_history(engine: AsyncEngine) -> None:
    thread = await seed_retained_session(engine)
    store = ThreadStore(engine)
    with pytest.raises(ValueError, match="missing service projection metadata"):
        await store.get_thread(thread)
    with pytest.raises(ValueError, match="missing service projection metadata"):
        await store.list_threads()


if __name__ == "__main__":
    pytest_bazel.main()
