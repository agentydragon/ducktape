"""The store's contract: a thread per session, events kept verbatim and idempotently, read back
without a runner."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from x.agentplane.app.trajectory import (
    FeedEnd,
    FeedError,
    IngestionLease,
    IngestionLeaseLostError,
    SandboxIngestion,
    ThreadNotFoundError,
    TrajectoryStore,
)
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SPEC = pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/state/work", model="test-model", reasoning_effort="low")


@pytest.fixture
async def lease(store: TrajectoryStore) -> IngestionLease:
    lease = await store.acquire_ingestion("sb-1", timedelta(minutes=1))
    assert lease is not None
    return lease


@pytest.fixture
async def replica(db_url: str) -> AsyncIterator[TrajectoryStore]:
    replica = TrajectoryStore.connect(db_url)
    await replica.ensure_schema()
    await replica.start_updates()
    try:
        yield replica
    finally:
        await replica.close()


def _event(sequence: int, **observation: object) -> pb.Event:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 2, 12, 0, sequence, tzinfo=UTC))
    return pb.Event(sequence=sequence, at=at, **observation)  # type: ignore[arg-type]


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
            _event(1, harness_started=pb.HarnessStarted(resumed=False, pid=7)),
            _event(2, native=pb.Native(direction=pb.DIRECTION_FROM_HARNESS, line='{"type":"x"}')),
            _event(3, turn_started=pb.TurnStarted(turn_id="t1")),
        ],
        lease=lease,
    )
    # A replay after a reconnect brings sequences already stored: they are not written twice.
    await store.record(
        thread,
        [_event(3, turn_started=pb.TurnStarted(turn_id="t1")), _event(4, harness_lost=pb.HarnessLost())],
        lease=lease,
    )

    events = await store.events(thread, limit=100)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert events[1].native.line == '{"type":"x"}'
    assert events[1].at.ToDatetime(tzinfo=UTC) == datetime(2026, 9, 2, 12, 0, 2, tzinfo=UTC)
    assert [event.sequence for event in await store.events(thread, after_sequence=2, limit=100)] == [3, 4]
    assert [event.sequence for event in await store.events(thread, after_sequence=1, limit=2)] == [2, 3]
    assert await store.last_sequence(thread) == 4
    assert await store.last_sequence(other) == 0


async def test_threads_list_with_their_progress(store: TrajectoryStore, lease: IngestionLease) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    empty = await store.thread("sb-2", "s-9", pb.SessionSpec(provider=pb.PROVIDER_CODEX, cwd="/w", model="m"))
    await store.record(
        thread,
        [_event(1, harness_started=pb.HarnessStarted(pid=1)), _event(2, harness_lost=pb.HarnessLost())],
        lease=lease,
    )

    views = {view.id: view for view in await store.list_threads()}

    assert views[thread].model_dump(include={"sandbox", "session_id", "provider", "model", "cwd", "last_sequence"}) == {
        "sandbox": "sb-1",
        "session_id": "s-1",
        "provider": "PROVIDER_CLAUDE",
        "model": "test-model",
        "cwd": "/state/work",
        "last_sequence": 2,
    }
    assert views[thread].last_event_at == datetime(2026, 9, 2, 12, 0, 2, tzinfo=UTC)
    assert (views[empty].provider, views[empty].last_sequence, views[empty].last_event_at) == (
        "PROVIDER_CODEX",
        0,
        None,
    )
    assert await store.get_thread(empty) == views[empty]
    assert await store.get_thread(thread) == views[thread]
    assert [view.id for view in await store.list_threads(sandbox="sb-1")] == [thread]
    assert [view.id for view in await store.list_threads(sandbox="sb-2", session_id="s-9")] == [empty]
    assert await store.list_threads(sandbox="sb-1", session_id="s-9") == []


async def test_a_thread_is_unnamed_until_renamed_and_keeps_its_progress(
    store: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    await store.record(thread, [_event(1, harness_started=pb.HarnessStarted(pid=1))], lease=lease)
    (unnamed,) = await store.list_threads()
    assert unnamed.name is None

    renamed = await store.rename(thread, "list the files")

    assert (renamed.name, renamed.last_sequence) == ("list the files", 1)
    assert await store.get_thread(thread) == renamed
    assert (await store.rename(thread, None)).name is None
    with pytest.raises(ThreadNotFoundError):
        await store.rename(UUID(int=0), "nobody")


async def test_ensure_schema_adds_the_name_column_to_a_table_created_without_it(db_url: str) -> None:
    """create_all never alters an existing table, and staging already had threads before names."""
    store = TrajectoryStore.connect(db_url)
    try:
        await store.ensure_schema()
        older = create_async_engine(db_url)
        async with older.begin() as connection:
            await connection.execute(text("ALTER TABLE thread DROP COLUMN name"))
        await older.dispose()
        await store.ensure_schema()
        thread = await store.thread("sb-1", "s-1", SPEC)
        assert (await store.rename(thread, "after the alter")).name == "after the alter"
    finally:
        await store.close()


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
        await store.record(thread, [_event(1, harness_lost=pb.HarnessLost())], lease=lease)
        await asyncio.wait_for(changed.wait(), timeout=5)
        assert [event.sequence for event in await replica.events(thread, limit=10)] == [1]


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
            await store.record(thread, [_event(1, harness_lost=pb.HarnessLost())], lease=lease)
        await replica.record(thread, [_event(1, harness_lost=pb.HarnessLost())], lease=successor)
        wrong_thread = await store.thread("sb-2", "s-2", SPEC)
        with pytest.raises(IngestionLeaseLostError):
            await replica.record(wrong_thread, [_event(2, harness_lost=pb.HarnessLost())], lease=successor)
        assert await store.last_sequence(wrong_thread) == 0
        await replica.release_ingestion(successor)
        assert await store.acquire_ingestion("sb-1", timedelta(minutes=1)) is not None
    finally:
        await engine.dispose()


async def test_feed_attachment_and_terminal_state_survive_the_owner(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    assert await replica.feed_state(thread) is None
    attached = pb.Attached(session_id="s-1", spec=SPEC, last_sequence=7)
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


async def test_ingested_events_project_the_durable_attachment_without_replay_regression(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    attached = pb.Attached(session_id="s-1", spec=SPEC, harness=pb.HARNESS_STATE_STOPPED)
    await store.set_attached(thread, attached, lease=lease)
    await store.end_feed(thread, lease=lease, error=None)
    await store.record(
        thread,
        [
            _event(1, harness_started=pb.HarnessStarted(resumed=True)),
            _event(2, turn_started=pb.TurnStarted(turn_id="test-turn", model=SPEC.model)),
            _event(3, native=pb.Native(direction=pb.DIRECTION_FROM_HARNESS, line="test native frame")),
        ],
        lease=lease,
    )
    running = await replica.feed_state(thread)
    assert running is not None
    assert running.end is None
    assert running.attached.harness == pb.HARNESS_STATE_RUNNING
    assert running.attached.active_turn_id == "test-turn"
    assert running.attached.last_sequence == 3
    assert running.attached.spec == SPEC
    await store.record(
        thread,
        [
            _event(4, turn_completed=pb.TurnCompleted(turn_id="test-turn", status=pb.TURN_STATUS_COMPLETED)),
            _event(5, model_switch_rejected=pb.ModelSwitchRejected(switch_id="test-rejected", reason="no")),
            _event(6, model_switch_succeeded=pb.ModelSwitchSucceeded(model="test-next-model")),
            _event(7, harness_exited=pb.HarnessExited()),
        ],
        lease=lease,
    )
    await store.end_feed(thread, lease=lease, error=None)
    stopped = await replica.feed_state(thread)
    assert stopped is not None
    assert isinstance(stopped.end, FeedEnd)
    assert stopped.attached.harness == pb.HARNESS_STATE_STOPPED
    assert stopped.attached.active_turn_id == ""
    assert stopped.attached.spec.model == "test-next-model"
    assert stopped.attached.spec.reasoning_effort == SPEC.reasoning_effort
    assert stopped.attached.last_sequence == 7
    view = await replica.get_thread(thread)
    assert view is not None
    assert view.model == "test-next-model"
    # Even a contradictory duplicate payload cannot update the committed projection.
    await store.record(thread, [_event(7, harness_started=pb.HarnessStarted())], lease=lease)
    assert await replica.feed_state(thread) == stopped
    with pytest.raises(ValueError, match="older"):
        await store.set_attached(thread, attached, lease=lease)
    assert await replica.feed_state(thread) == stopped
    await store.record(thread, [_event(8, harness_started=pb.HarnessStarted(resumed=True))], lease=lease)
    resumed = await replica.feed_state(thread)
    assert resumed is not None
    assert resumed.end is None
    assert resumed.attached.harness == pb.HARNESS_STATE_RUNNING
    await store.record(thread, [_event(9, harness_lost=pb.HarnessLost())], lease=lease)
    lost = await replica.feed_state(thread)
    assert lost is not None
    assert lost.attached.harness == pb.HARNESS_STATE_STOPPED
    assert lost.attached.last_sequence == 9


async def test_historical_catchup_does_not_rewind_an_attachment_snapshot(
    store: TrajectoryStore, replica: TrajectoryStore, lease: IngestionLease
) -> None:
    thread = await store.thread("sb-1", "s-1", SPEC)
    attached = pb.Attached(session_id="s-1", spec=SPEC, harness=pb.HARNESS_STATE_STOPPED, last_sequence=2)
    await store.set_attached(thread, attached, lease=lease)
    await store.end_feed(thread, lease=lease, error=None)
    await store.record(thread, [_event(1, harness_started=pb.HarnessStarted())], lease=lease)
    caught_up = await replica.feed_state(thread)
    assert caught_up is not None
    assert caught_up.attached == attached
    assert isinstance(caught_up.end, FeedEnd)
    assert await store.last_sequence(thread) == 1


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
            write = asyncio.create_task(store.record(thread, [_event(1, harness_lost=pb.HarnessLost())], lease=lease))
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
        assert await store.last_sequence(thread) == 0
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
            # The termination callback wakes local consumers before the reconnect attempt.
            await asyncio.wait_for(changed.wait(), timeout=5)
            changed.clear()
            await store.record(thread, [_event(1, harness_lost=pb.HarnessLost())], lease=lease)
            await asyncio.wait_for(changed.wait(), timeout=5)
            assert [event.sequence for event in await replica.events(thread, limit=10)] == [1]
            # Establish another live write still wakes this replica after it reconnects.
            changed.clear()
            await store.rename(thread, "after reconnect")
            await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
