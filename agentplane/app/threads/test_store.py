"""The thread level: threads listed with their progress, and the name and archive state an operator
sets on one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.conftest import SPEC, event_entry
from agentplane.app.threads.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.threads.events.ingestion_lease import IngestionLease
from agentplane.app.threads.ingestion import Ingestion
from agentplane.app.threads.store import ThreadStore
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.harness import Harness

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_threads_list_with_their_progress(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    empty = await event_logs.open(
        "sb-2", "s-9", protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CODEX, cwd="/w", model="m")
    )
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    second = event_entry(2, harness_lost=event_pb2.HarnessLost())
    second.event.at.FromDatetime(datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC))
    await ingestion.record(thread, [first, second], lease=lease)

    views = {view.id: view for view in await store.list_threads()}

    assert views[thread].model_dump(include={"sandbox", "session_id", "harness", "model", "cwd", "last_cursor"}) == {
        "sandbox": "sb-1",
        "session_id": "s-1",
        "harness": "HARNESS_CLAUDE",
        "model": "test-model",
        "cwd": "/state/work",
        "last_cursor": 2,
    }
    assert views[thread].harness is Harness.CLAUDE
    assert views[thread].last_event_at == datetime(2026, 9, 2, 12, 0, 1, tzinfo=UTC)
    assert (views[empty].harness, views[empty].last_cursor, views[empty].last_event_at) == ("HARNESS_CODEX", 0, None)
    assert views[empty].harness is Harness.CODEX
    assert views[thread].feed_status is None
    assert views[empty].feed_status is None
    # No feed has ever attached to either thread (only their event log was replayed), so the
    # exposed harness state stays unspecified rather than inferring it from history.
    assert views[thread].harness_state == views[empty].harness_state == "HARNESS_STATE_UNSPECIFIED"
    assert await store.get_thread(empty) == views[empty]
    assert await store.get_thread(thread) == views[thread]
    assert [view.id for view in await store.list_threads(sandbox="sb-1")] == [thread]
    assert [view.id for view in await store.list_threads(sandbox="sb-2", session_id="s-9")] == [empty]
    assert await store.list_threads(sandbox="sb-1", session_id="s-9") == []
    async with store._sessions() as session:
        await session.execute(text("SET LOCAL enable_seqscan = false"))
        plan = (
            await session.scalars(
                text("EXPLAIN (COSTS OFF) SELECT at FROM event WHERE thread_id = :thread ORDER BY at DESC LIMIT 1"),
                {"thread": thread},
            )
        ).all()
    assert any("ix_event_thread_at" in line for line in plan)


async def test_threads_list_reflects_attached_harness_and_active_turn_state(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    """The sidebar snapshot carries the latest attached harness and turn state, not just history."""
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    running_attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
    )
    await ingestion.set_attached(thread, running_attached, lease=lease)
    await ingestion.record(thread, [event_entry(1, turn_started=event_pb2.TurnStarted(turn_id="turn-1"))], lease=lease)

    (running,) = await store.list_threads()
    assert running.harness_state == "HARNESS_STATE_RUNNING"
    assert running.feed_status == "active"
    assert running.reasoning_effort == SPEC.reasoning_effort
    assert running.active_turn_id == "turn-1"
    got_thread = await store.get_thread(thread)
    assert got_thread is not None
    assert got_thread.harness_state == "HARNESS_STATE_RUNNING"
    assert got_thread.active_turn_id == "turn-1"

    await ingestion.record(
        thread,
        [
            event_entry(
                2, turn_completed=event_pb2.TurnCompleted(turn_id="turn-1", status=event_pb2.TURN_STATUS_COMPLETED)
            )
        ],
        lease=lease,
    )
    (idle,) = await store.list_threads()
    assert idle.harness_state == "HARNESS_STATE_RUNNING"
    assert idle.active_turn_id is None

    await ingestion.record(
        thread,
        [
            event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="turn-2")),
            event_entry(4, harness_lost=event_pb2.HarnessLost()),
        ],
        lease=lease,
    )
    (lost,) = await store.list_threads()
    assert lost.harness_state == "HARNESS_STATE_STOPPED"
    assert lost.active_turn_id is None

    stopped_attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, last_cursor=4, harness_state=protocol_pb2.HARNESS_STATE_STOPPED
    )
    await ingestion.set_attached(thread, stopped_attached, lease=lease)
    (stopped,) = await store.list_threads()
    assert stopped.harness_state == "HARNESS_STATE_STOPPED"
    assert stopped.active_turn_id is None

    await ingestion.end_feed(thread, lease=lease, error=None)
    (ended,) = await store.list_threads()
    assert ended.feed_status == "ended"
    await ingestion.set_attached(thread, stopped_attached, lease=lease)
    await ingestion.end_feed(thread, lease=lease, error="runner feed failed")
    failed = await store.get_thread(thread)
    assert failed is not None
    assert failed.feed_status == "failed"


COMPLETED = event_pb2.TurnCompleted(turn_id="turn-completed", status=event_pb2.TURN_STATUS_COMPLETED)
FAILED = event_pb2.TurnCompleted(turn_id="turn-failed", status=event_pb2.TURN_STATUS_FAILED)
LOST = event_pb2.TurnCompleted(turn_id="turn-lost", status=event_pb2.TURN_STATUS_PROCESS_LOST)


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        (COMPLETED, "TURN_STATUS_COMPLETED"),
        (event_pb2.TurnCompleted(status=event_pb2.TURN_STATUS_INTERRUPTED), "TURN_STATUS_INTERRUPTED"),
        (FAILED, "TURN_STATUS_FAILED"),
        (LOST, "TURN_STATUS_PROCESS_LOST"),
        # Proto-JSON leaves a default out of the stored payload, so there is no `status` key to read.
        (event_pb2.TurnCompleted(turn_id="turn-unspecified"), "TURN_STATUS_UNSPECIFIED"),
    ],
)
async def test_a_thread_reports_how_its_last_completed_turn_ended(
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    lease: IngestionLease,
    turn: event_pb2.TurnCompleted,
    expected: str,
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(
        thread,
        [event_entry(1, turn_started=event_pb2.TurnStarted(turn_id=turn.turn_id)), event_entry(2, turn_completed=turn)],
        lease=lease,
    )

    (listed,) = await store.list_threads()

    assert listed.last_turn_status == expected
    assert await store.get_thread(thread) == listed


async def test_a_thread_has_no_last_turn_status_until_a_turn_completes(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    await event_logs.open("sb-1", "empty", SPEC)
    started = await event_logs.open("sb-1", "started", SPEC)
    await ingestion.record(started, [event_entry(1, turn_started=event_pb2.TurnStarted(turn_id="turn-1"))], lease=lease)

    assert {view.session_id: view.last_turn_status for view in await store.list_threads()} == {
        "empty": None,
        "started": None,
    }


async def test_the_last_turn_status_is_the_most_recent_completed_turn_of_that_thread(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    recovered = await event_logs.open("sb-1", "recovered", SPEC)
    failing = await event_logs.open("sb-1", "failing", SPEC)
    await ingestion.record(recovered, [event_entry(1, turn_completed=FAILED)], lease=lease)
    await ingestion.record(failing, [event_entry(1, turn_completed=COMPLETED)], lease=lease)

    async def statuses() -> dict[str, str | None]:
        return {view.session_id: view.last_turn_status for view in await store.list_threads()}

    assert await statuses() == {"recovered": "TURN_STATUS_FAILED", "failing": "TURN_STATUS_COMPLETED"}

    await ingestion.record(
        recovered,
        [event_entry(2, turn_completed=COMPLETED), event_entry(3, harness_started=event_pb2.HarnessStarted(pid=1))],
        lease=lease,
    )
    await ingestion.record(failing, [event_entry(2, turn_completed=LOST)], lease=lease)

    # A later event of another kind leaves the outcome alone; a later completed turn replaces it.
    assert await statuses() == {"recovered": "TURN_STATUS_COMPLETED", "failing": "TURN_STATUS_PROCESS_LOST"}


async def test_the_last_turn_lookup_uses_the_turn_completion_index_under_a_long_log(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease, engine: AsyncEngine
) -> None:
    """The thread list is rebuilt on every ingested batch, so a thread's newest completed turn must be
    found without walking the log behind it."""
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    trace = event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"type":"trace"}')
    await ingestion.record(
        thread,
        [event_entry(1, turn_completed=FAILED), *(event_entry(cursor, native=trace) for cursor in range(2, 302))],
        lease=lease,
    )
    statements: list[tuple[str, Any]] = []

    def capture(_: object, __: object, statement: str, parameters: Any, ___: object, ____: bool) -> None:
        statements.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        (listed,) = await store.list_threads()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert listed.last_turn_status == "TURN_STATUS_FAILED"

    ((statement, parameters),) = statements
    async with engine.connect() as connection:
        await connection.exec_driver_sql("ANALYZE event")
        await connection.exec_driver_sql("SET enable_seqscan = off")
        plan = [row[0] for row in await connection.exec_driver_sql(f"EXPLAIN (COSTS OFF) {statement}", parameters)]
    assert any("ix_event_thread_turn_completed" in line for line in plan), plan


async def test_a_thread_is_unnamed_until_renamed_and_keeps_its_progress(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))], lease=lease)
    (unnamed,) = await store.list_threads()
    assert unnamed.name is None

    renamed = await store.rename(thread, "list the files")

    assert (renamed.name, renamed.last_cursor) == ("list the files", 1)
    assert await store.get_thread(thread) == renamed
    assert (await store.rename(thread, None)).name is None
    with pytest.raises(ThreadNotFoundError):
        await store.rename(UUID(int=0), "nobody")


async def test_a_thread_archives_and_unarchives_without_touching_its_progress(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))], lease=lease)
    (unarchived,) = await store.list_threads()
    assert unarchived.archived is False

    archived = await store.archive(thread)

    assert (archived.archived, archived.last_cursor) == (True, 1)
    assert await store.get_thread(thread) == archived
    assert await store.list_threads() == []
    assert [view.id for view in await store.list_threads(include_archived=True)] == [thread]

    unarchived = await store.unarchive(thread)
    assert unarchived.archived is False
    assert [view.id for view in await store.list_threads()] == [thread]
    with pytest.raises(ThreadNotFoundError):
        await store.archive(UUID(int=0))


if __name__ == "__main__":
    pytest_bazel.main()
