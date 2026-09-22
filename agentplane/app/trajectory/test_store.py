"""The store's contract: a thread per session, events kept verbatim and idempotently, read back
without a runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agentplane.app.conftest import SPEC, event_entry
from agentplane.app.presets import Harness
from agentplane.app.trajectory.models import SandboxIngestion
from agentplane.app.trajectory.recording import EventReplicationError
from agentplane.app.trajectory.store import (
    CommandIdConflictError,
    FeedEnd,
    FeedError,
    IngestionLease,
    IngestionLeaseLostError,
    ThreadNotFoundError,
    TrajectoryStore,
)
from agentplane.app.trajectory.updates import notify
from agentplane.protocol import command_pb2, event_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_a_session_is_one_thread_and_its_events_read_back_in_order(
    store: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    assert await store.thread("sb-1", "s-1", SPEC) == thread
    other = await store.thread("sb-1", "s-2", SPEC)
    assert other != thread

    await store.record(
        thread,
        [
            event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7)),
            event_entry(2, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"type":"x"}')),
            event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="t1")),
        ],
        lease=lease,
    )
    # A replay after a reconnect brings sequences already stored: they are not written twice.
    await store.record(
        thread,
        [
            event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="t1")),
            event_entry(4, harness_lost=event_pb2.HarnessLost()),
        ],
        lease=lease,
    )

    entries = await store.events(thread, limit=100)
    assert [entry.cursor for entry in entries] == [1, 2, 3, 4]
    assert entries[1].event.native.line == '{"type":"x"}'
    assert entries[1].event.at.ToDatetime(tzinfo=UTC) == datetime(2026, 9, 2, 12, 0, 2, tzinfo=UTC)
    assert [entry.cursor for entry in await store.events(thread, after_cursor=2, limit=100)] == [3, 4]
    assert [entry.cursor for entry in await store.events(thread, after_cursor=1, limit=2)] == [2, 3]
    assert await store.last_cursor(thread) == 4
    assert await store.last_cursor(other) == 0


async def test_archived_command_admission_is_an_exact_retry_key(store: TrajectoryStore, lease: IngestionLease) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    command = command_pb2.Command(
        command_id="submit-1", submit_input=command_pb2.SubmitInput(text="persist this exact input")
    )
    admitted = event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command))
    await store.record(thread, [admitted], lease=lease)

    # The saved runner Event is the retry receipt, including its runner origin/cursor; it is not
    # rebuilt from a separate app command row.
    assert await store.admitted_command(thread, command) == admitted
    with pytest.raises(CommandIdConflictError, match="different work"):
        await store.admitted_command(
            thread, command_pb2.Command(command_id="submit-1", submit_input=command_pb2.SubmitInput(text="other input"))
        )
    with pytest.raises(ThreadNotFoundError):
        await store.admitted_command(UUID(int=0), command)


async def test_threads_list_with_their_progress(store: TrajectoryStore, lease: IngestionLease) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    empty = await store.thread(
        "sb-2", "s-9", protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CODEX, cwd="/w", model="m")
    )
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    second = event_entry(2, harness_lost=event_pb2.HarnessLost())
    second.event.at.FromDatetime(datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC))
    await store.record(thread, [first, second], lease=lease)

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


@pytest.mark.parametrize("cursors", [pytest.param([2], id="initial-gap"), pytest.param([1, 3], id="batch-gap"), [2, 1]])
async def test_gapped_batches_leave_no_archived_prefix(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease, cursors: list[int]
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    with pytest.raises(EventReplicationError, match="expected runner cursor"):
        await store.record(
            thread, [event_entry(cursor, harness_lost=event_pb2.HarnessLost()) for cursor in cursors], lease=lease
        )
    assert await replica.events(thread, limit=10) == []
    assert await replica.last_cursor(thread) == 0


async def test_conflicting_replay_rolls_back_the_whole_batch(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    attached = protocol_pb2.Attached(session_id="s-1", spec=SPEC)
    await store.set_attached(thread, attached, lease=lease)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    await store.record(thread, [first], lease=lease)
    before = await replica.feed_state(thread)
    with pytest.raises(EventReplicationError, match="conflicting runner entry"):
        await store.record(
            thread,
            [
                event_entry(2, model_changed=event_pb2.ModelChanged(model="test-rejected-model")),
                event_entry(1, harness_started=event_pb2.HarnessStarted(pid=2)),
            ],
            lease=lease,
        )
    assert await replica.events(thread, limit=10) == [first]
    assert await replica.last_cursor(thread) == 1
    assert await replica.feed_state(thread) == before
    view = await replica.get_thread(thread)
    assert view is not None
    assert view.model == SPEC.model


@pytest.mark.parametrize(
    ("cursor", "source_id", "sequence"),
    [(0, "test-runner", 0), (2, "", 2), (2, "test-runner", 1), (2, "test-replacement-source", 2)],
)
async def test_origin_must_match_the_original_runner_log(
    store: TrajectoryStore, lease: IngestionLease, cursor: int, source_id: str, sequence: int
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted())
    await store.record(thread, [first], lease=lease)
    wrong = event_entry(cursor, harness_lost=event_pb2.HarnessLost())
    wrong.origin.source_id = source_id
    wrong.origin.sequence = sequence
    with pytest.raises(EventReplicationError):
        await store.record(thread, [wrong], lease=lease)
    assert await store.events(thread, limit=10) == [first]


async def test_same_batch_duplicates_require_identical_payloads(store: TrajectoryStore, lease: IngestionLease) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    second = event_entry(2, harness_lost=event_pb2.HarnessLost())
    with pytest.raises(EventReplicationError, match="conflicting runner entry"):
        await store.record(
            thread, [first, event_entry(1, harness_started=event_pb2.HarnessStarted(pid=2))], lease=lease
        )
    assert await store.events(thread, limit=10) == []
    await store.record(thread, [first, first, second, second], lease=lease)
    assert await store.events(thread, limit=10) == [first, second]


async def test_competing_copies_cannot_replace_an_archived_entry(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted())
    await asyncio.gather(store.record(thread, [first], lease=lease), replica.record(thread, [first], lease=lease))
    contenders = [
        event_entry(2, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line=line))
        for line in ["test-frame-A", "test-frame-B"]
    ]
    results = await asyncio.gather(
        store.record(thread, [contenders[0]], lease=lease),
        replica.record(thread, [contenders[1]], lease=lease),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, EventReplicationError) for result in results) == 1
    winner = contenders[results.index(None)]
    assert await replica.events(thread, limit=10) == [first, winner]
    assert await replica.last_cursor(thread) == 2


async def test_connection_loss_before_commit_keeps_events_projection_and_cursor_atomic(
    store: TrajectoryStore,
    replica: TrajectoryStore,
    lease: IngestionLease,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    await store.set_attached(thread, protocol_pb2.Attached(session_id="s-1", spec=SPEC), lease=lease)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted())
    await store.record(thread, [first], lease=lease)
    before = await replica.feed_state(thread)
    second = event_entry(2, model_changed=event_pb2.ModelChanged(model="test-committed-model"))
    engine = create_async_engine(db_url)

    async def disconnect_before_commit(session: AsyncSession) -> None:
        await notify(session)
        assert await replica.events(thread, limit=10) == [first]
        assert await replica.last_cursor(thread) == 1
        assert await replica.feed_state(thread) == before
        pid = await session.scalar(select(func.pg_backend_pid()))
        async with engine.begin() as connection:
            assert await connection.scalar(select(func.pg_terminate_backend(pid, 5000)))

    try:
        with monkeypatch.context() as patch:
            patch.setattr("agentplane.app.trajectory.store.notify", disconnect_before_commit)
            with pytest.raises(DBAPIError):
                await store.record(thread, [second], lease=lease)
        assert await replica.events(thread, limit=10) == [first]
        assert await replica.last_cursor(thread) == 1
        assert await replica.feed_state(thread) == before
        await replica.record(thread, [first, second], lease=lease)
        assert await store.events(thread, limit=10) == [first, second]
        assert await store.last_cursor(thread) == 2
        view = await store.get_thread(thread)
        assert view is not None
        assert view.model == "test-committed-model"
    finally:
        await engine.dispose()


async def test_threads_list_reflects_the_attached_feed_s_harness_state(
    store: TrajectoryStore, lease: IngestionLease
) -> None:
    """`list_threads`/`get_thread` expose `FeedState.attached.harness_state` per thread — the live
    running/idle signal the sidebar's per-thread status dot reads (`agentplane/plans/task_dag.md`
    `UISHELL_SIDEBAR`), not a value derived from the historical event log."""
    thread = await store.thread("sb-1", "s-1", SPEC)
    running_attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
    )
    await store.set_attached(thread, running_attached, lease=lease)

    (running,) = await store.list_threads()
    assert running.harness_state == "HARNESS_STATE_RUNNING"
    got_thread = await store.get_thread(thread)
    assert got_thread is not None
    assert got_thread.harness_state == "HARNESS_STATE_RUNNING"

    stopped_attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_STOPPED
    )
    await store.set_attached(thread, stopped_attached, lease=lease)
    (stopped,) = await store.list_threads()
    assert stopped.harness_state == "HARNESS_STATE_STOPPED"


async def test_a_thread_is_unnamed_until_renamed_and_keeps_its_progress(
    store: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    await store.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))], lease=lease)
    (unnamed,) = await store.list_threads()
    assert unnamed.name is None

    renamed = await store.rename(thread, "list the files")

    assert (renamed.name, renamed.last_cursor) == ("list the files", 1)
    assert await store.get_thread(thread) == renamed
    assert (await store.rename(thread, None)).name is None
    with pytest.raises(ThreadNotFoundError):
        await store.rename(UUID(int=0), "nobody")


async def test_a_thread_archives_and_unarchives_without_touching_its_progress(
    store: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    await store.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))], lease=lease)
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


async def test_concurrent_replicas_create_one_thread(store: TrajectoryStore, replica: TrajectoryStore) -> None:
    first, second = await asyncio.gather(store.thread("sb-1", "s-1", SPEC), replica.thread("sb-1", "s-1", SPEC))
    assert first == second
    assert len(await replica.list_threads()) == 1


async def test_concurrent_replicas_choose_one_ingester(store: TrajectoryStore, replica: TrajectoryStore) -> None:
    first, second = await asyncio.gather(
        store.acquire_ingestion("test-racing-sandbox", timedelta(minutes=1)),
        replica.acquire_ingestion("test-racing-sandbox", timedelta(minutes=1)),
    )
    assert (first is None) != (second is None)


async def test_commits_wake_another_replica_and_leave_durable_replay(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    changed = asyncio.Event()
    with replica.changes.subscribe(changed):
        thread = await store.thread("sb-1", "s-1", SPEC)
        await asyncio.wait_for(changed.wait(), timeout=5)
        assert (await replica.list_threads())[0].id == thread
        changed.clear()
        await store.rename(thread, "cross-replica rename")
        await asyncio.wait_for(changed.wait(), timeout=5)
        view = await replica.get_thread(thread)
        assert view is not None
        assert view.name == "cross-replica rename"
        changed.clear()
        await store.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
        await asyncio.wait_for(changed.wait(), timeout=5)
        assert [entry.cursor for entry in await replica.events(thread, limit=10)] == [1]


async def test_only_current_lease_can_write_or_renew(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease, db_url: str
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    assert await replica.acquire_ingestion("sb-1", timedelta(minutes=1)) is None
    assert await store.renew_ingestion(lease, timedelta(minutes=2))
    # Use database time without sleeps: make the first owner stale while retaining its token.
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                update(SandboxIngestion)
                .where(SandboxIngestion.sandbox == "sb-1")
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        assert not await store.renew_ingestion(lease, timedelta(minutes=1))
        successor = await replica.acquire_ingestion("sb-1", timedelta(minutes=1))
        assert successor is not None
        assert successor.token != lease.token
        await store.release_ingestion(lease)
        assert await replica.renew_ingestion(successor, timedelta(minutes=1))
        with pytest.raises(IngestionLeaseLostError):
            await store.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
        await replica.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=successor)
        wrong_thread = await store.thread("sb-2", "s-2", SPEC)
        with pytest.raises(IngestionLeaseLostError):
            await replica.record(wrong_thread, [event_entry(2, harness_lost=event_pb2.HarnessLost())], lease=successor)
        assert await store.last_cursor(wrong_thread) == 0
        await replica.release_ingestion(successor)
        assert await store.acquire_ingestion("sb-1", timedelta(minutes=1)) is not None
    finally:
        await engine.dispose()


async def test_feed_attachment_and_terminal_state_survive_the_owner(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    assert await replica.feed_state(thread) is None
    attached = protocol_pb2.Attached(session_id="s-1", spec=SPEC, last_cursor=7)
    await store.set_attached(thread, attached, lease=lease)
    snapshot = await replica.feed_state(thread)
    assert snapshot is not None
    assert snapshot.attached == attached
    assert snapshot.end is None
    await store.end_feed(thread, lease=lease, error=None)
    ended = await replica.feed_state(thread)
    assert ended is not None
    assert isinstance(ended.end, FeedEnd)
    await store.set_attached(thread, attached, lease=lease)
    await store.end_feed(thread, lease=lease, error="test runner refused attachment")
    failed = await replica.feed_state(thread)
    assert failed is not None
    assert failed.end == FeedError("test runner refused attachment")
    await store.release_ingestion(lease)
    with pytest.raises(IngestionLeaseLostError):
        await store.set_attached(thread, attached, lease=lease)
    with pytest.raises(IngestionLeaseLostError):
        await store.end_feed(thread, lease=lease, error=None)


async def test_rejected_entry_inside_a_batch_carries_its_cursor(store: TrajectoryStore, lease: IngestionLease) -> None:
    thread = await store.thread("sb-1", "s-invalid-origin", SPEC)
    rejected = event_entry(2, harness_started=event_pb2.HarnessStarted())
    rejected.origin.sequence = 3
    with pytest.raises(EventReplicationError, match="invalid runner origin at cursor 2") as raised:
        await store.record(
            thread,
            [
                event_entry(1, harness_started=event_pb2.HarnessStarted()),
                rejected,
                event_entry(3, harness_started=event_pb2.HarnessStarted()),
            ],
            lease=lease,
        )
    assert raised.value.cursor == 2
    assert await store.last_cursor(thread) == 0


async def test_ingested_events_project_the_durable_attachment_without_replay_regression(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    attached = protocol_pb2.Attached(session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_STOPPED)
    await store.set_attached(thread, attached, lease=lease)
    await store.end_feed(thread, lease=lease, error=None)
    await store.record(
        thread,
        [
            event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=True)),
            event_entry(2, turn_started=event_pb2.TurnStarted(turn_id="test-turn", model=SPEC.model)),
            event_entry(
                3, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="test native frame")
            ),
        ],
        lease=lease,
    )
    running = await replica.feed_state(thread)
    assert running is not None
    assert running.end is None
    assert running.attached.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
    assert running.attached.active_turn_id == "test-turn"
    assert running.attached.last_cursor == 3
    assert running.attached.spec == SPEC
    await store.record(
        thread,
        [
            event_entry(
                4,
                command_admitted=event_pb2.CommandAdmitted(
                    command=command_pb2.Command(
                        command_id="test-failed", interrupt_turn=command_pb2.InterruptTurn(turn_id="test-turn")
                    )
                ),
            ),
            event_entry(
                5, turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=event_pb2.TURN_STATUS_COMPLETED)
            ),
            event_entry(6, command_failed=event_pb2.CommandFailed(command_id="test-failed", reason="no")),
            event_entry(7, model_changed=event_pb2.ModelChanged(model="test-next-model")),
            event_entry(8, harness_exited=event_pb2.HarnessExited()),
        ],
        lease=lease,
    )
    await store.end_feed(thread, lease=lease, error=None)
    stopped = await replica.feed_state(thread)
    assert stopped is not None
    assert isinstance(stopped.end, FeedEnd)
    assert stopped.attached.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    assert stopped.attached.active_turn_id == ""
    assert stopped.attached.spec.model == "test-next-model"
    assert stopped.attached.spec.reasoning_effort == SPEC.reasoning_effort
    assert stopped.attached.last_cursor == 8
    view = await replica.get_thread(thread)
    assert view is not None
    assert view.model == "test-next-model"
    with pytest.raises(EventReplicationError, match="conflicting runner entry"):
        await store.record(thread, [event_entry(8, harness_started=event_pb2.HarnessStarted())], lease=lease)
    assert await replica.feed_state(thread) == stopped
    with pytest.raises(ValueError, match="older"):
        await store.set_attached(thread, attached, lease=lease)
    assert await replica.feed_state(thread) == stopped
    await store.record(thread, [event_entry(9, harness_started=event_pb2.HarnessStarted(resumed=True))], lease=lease)
    resumed = await replica.feed_state(thread)
    assert resumed is not None
    assert resumed.end is None
    assert resumed.attached.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
    await store.record(thread, [event_entry(10, harness_lost=event_pb2.HarnessLost())], lease=lease)
    lost = await replica.feed_state(thread)
    assert lost is not None
    assert lost.attached.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    assert lost.attached.last_cursor == 10


async def test_historical_catchup_does_not_rewind_an_attachment_snapshot(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_STOPPED, last_cursor=2
    )
    await store.set_attached(thread, attached, lease=lease)
    await store.end_feed(thread, lease=lease, error=None)
    await store.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted())], lease=lease)
    caught_up = await replica.feed_state(thread)
    assert caught_up is not None
    assert caught_up.attached == attached
    assert isinstance(caught_up.end, FeedEnd)
    assert await store.last_cursor(thread) == 1


async def test_record_rechecks_expiry_after_waiting_for_the_lease_row(
    store: TrajectoryStore, lease: IngestionLease, db_url: str
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    engine = create_async_engine(db_url)
    write: asyncio.Task[None] | None = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                select(SandboxIngestion).where(SandboxIngestion.sandbox == lease.sandbox).with_for_update()
            )
            write = asyncio.create_task(
                store.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
            )
            async with asyncio.timeout(5):
                while True:
                    await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query LIKE 'SELECT sandbox_ingestion.%'"
                        )
                    )
                    if waiting:
                        break
            # Expire after record's transaction began. Transaction-start now() would accept the
            # write; clock_timestamp() checked after the lock must reject it.
            await connection.execute(
                update(SandboxIngestion)
                .where(SandboxIngestion.sandbox == lease.sandbox)
                .values(expires_at=func.clock_timestamp())
            )
        with pytest.raises(IngestionLeaseLostError):
            await write
        assert await store.last_cursor(thread) == 0
    finally:
        if write is not None and not write.done():
            write.cancel()
            await asyncio.gather(write, return_exceptions=True)
        await engine.dispose()


async def test_listener_reconnect_wakes_readers_for_writes_during_the_gap(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease, db_url: str
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    engine = create_async_engine(db_url)
    changed = asyncio.Event()
    try:
        with replica.changes.subscribe(changed):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND application_name = 'agentplane-trajectory-updates'"
                    )
                )
            # Wait on the termination callback itself, rather than an untagged Changes wakeup:
            # a previous database notification can arrive after the subscription starts.
            await asyncio.wait_for(replica._updates.wait_until_disconnected(), timeout=5)
            assert not replica.updates_connected
            changed.clear()
            await store.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
            await asyncio.wait_for(changed.wait(), timeout=5)
            assert replica.updates_connected
            assert [entry.cursor for entry in await replica.events(thread, limit=10)] == [1]
            # Establish another live write still wakes this replica after it reconnects.
            changed.clear()
            await store.rename(thread, "after reconnect")
            await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
