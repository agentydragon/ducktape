"""Ingestion: batches read off a runner's stream, and recording them under the sandbox's lease -- a
contiguous, verbatim prefix, the feed state beside it, and one owner at a time."""

from __future__ import annotations

import asyncio

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
from collections import deque
from contextlib import aclosing
from datetime import timedelta

import pytest
import pytest_bazel
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agentplane.app.agent_runtime.events.event_log import EventLogStore, EventReplicationError, FeedEnd, FeedError
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease, IngestionLeaseLostError
from agentplane.app.agent_runtime.ingestion import Ingestion, event_batches
from agentplane.app.agent_runtime.models import SandboxIngestion
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.agent_runtime.updates import notify
from agentplane.app.conftest import SPEC, Replica, event_entry
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import StreamClosedError

# gazelle:include_dep @pypi//protobuf


async def test_full_batches_and_eof_preserve_order() -> None:
    source = deque(event_log_pb2.EventEntry(cursor=i) for i in range(1, 302))

    async def read() -> event_log_pb2.EventEntry:
        if not source:
            raise StreamClosedError
        return source.popleft()

    batches = [batch async for batch in event_batches(read, limit=128, delay_s=60)]
    assert [len(batch) for batch in batches] == [128, 128, 45]
    assert [entry.cursor for batch in batches for entry in batch] == list(range(1, 302))


async def test_deadline_flush_does_not_cancel_or_replace_pending_read() -> None:
    gate = asyncio.Event()
    waiting = asyncio.Event()
    reads = 0
    cancellations = 0

    async def read() -> event_log_pb2.EventEntry:
        nonlocal reads, cancellations
        reads += 1
        if reads == 1:
            return event_log_pb2.EventEntry(cursor=1)
        if reads == 2:
            waiting.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancellations += 1
                raise
            return event_log_pb2.EventEntry(cursor=2)
        raise StreamClosedError

    async with aclosing(event_batches(read, delay_s=0.001)) as batches:
        first = await asyncio.wait_for(anext(batches), timeout=2)
        await waiting.wait()
        assert [entry.cursor for entry in first] == [1]
        assert reads == 2
        assert cancellations == 0
        gate.set()
        remaining = [entry.cursor async for batch in batches for entry in batch]
        assert remaining == [2]
    assert cancellations == 0


async def test_slow_consumer_has_only_one_read_ahead_and_close_cancels_it() -> None:
    count = 0
    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    async def read() -> event_log_pb2.EventEntry:
        nonlocal count
        count += 1
        if count <= 4:
            return event_log_pb2.EventEntry(cursor=count)
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    async with aclosing(event_batches(read, limit=4, delay_s=60)) as batches:
        page = await anext(batches)
        await waiting.wait()
        assert len(page) == 4
        assert count == 5
    assert cancelled.is_set()


@pytest.mark.parametrize("cursors", [pytest.param([2], id="initial-gap"), pytest.param([1, 3], id="batch-gap"), [2, 1]])
async def test_gapped_batches_leave_no_archived_prefix(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease, cursors: list[int]
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    with pytest.raises(EventReplicationError, match="expected runner cursor"):
        await ingestion.record(
            thread, [event_entry(cursor, harness_lost=event_pb2.HarnessLost()) for cursor in cursors], lease=lease
        )
    assert await replica.event_logs.events(thread, limit=10) == []
    assert await replica.event_logs.last_cursor(thread) == 0


async def test_conflicting_replay_rolls_back_the_whole_batch(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    attached = protocol_pb2.Attached(session_id="s-1", spec=SPEC)
    await ingestion.set_attached(thread, attached, lease=lease)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    await ingestion.record(thread, [first], lease=lease)
    before = await replica.event_logs.feed_state(thread)
    with pytest.raises(EventReplicationError, match="conflicting runner entry"):
        await ingestion.record(
            thread,
            [
                event_entry(2, model_changed=event_pb2.ModelChanged(model="test-rejected-model")),
                event_entry(1, harness_started=event_pb2.HarnessStarted(pid=2)),
            ],
            lease=lease,
        )
    assert await replica.event_logs.events(thread, limit=10) == [first]
    assert await replica.event_logs.last_cursor(thread) == 1
    assert await replica.event_logs.feed_state(thread) == before
    view = await replica.store.get_thread(thread)
    assert view is not None
    assert view.model == SPEC.model


@pytest.mark.parametrize(
    ("cursor", "source_id", "sequence"),
    [(0, "test-runner", 0), (2, "", 2), (2, "test-runner", 1), (2, "test-replacement-source", 2)],
)
async def test_origin_must_match_the_original_runner_log(
    event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease, cursor: int, source_id: str, sequence: int
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted())
    await ingestion.record(thread, [first], lease=lease)
    wrong = event_entry(cursor, harness_lost=event_pb2.HarnessLost())
    wrong.origin.source_id = source_id
    wrong.origin.sequence = sequence
    with pytest.raises(EventReplicationError):
        await ingestion.record(thread, [wrong], lease=lease)
    assert await event_logs.events(thread, limit=10) == [first]


async def test_same_batch_duplicates_require_identical_payloads(
    event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    second = event_entry(2, harness_lost=event_pb2.HarnessLost())
    with pytest.raises(EventReplicationError, match="conflicting runner entry"):
        await ingestion.record(
            thread, [first, event_entry(1, harness_started=event_pb2.HarnessStarted(pid=2))], lease=lease
        )
    assert await event_logs.events(thread, limit=10) == []
    await ingestion.record(thread, [first, first, second, second], lease=lease)
    assert await event_logs.events(thread, limit=10) == [first, second]


async def test_competing_copies_cannot_replace_an_archived_entry(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted())
    await asyncio.gather(
        ingestion.record(thread, [first], lease=lease), replica.ingestion.record(thread, [first], lease=lease)
    )
    contenders = [
        event_entry(2, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line=line))
        for line in ["test-frame-A", "test-frame-B"]
    ]
    results = await asyncio.gather(
        ingestion.record(thread, [contenders[0]], lease=lease),
        replica.ingestion.record(thread, [contenders[1]], lease=lease),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, EventReplicationError) for result in results) == 1
    winner = contenders[results.index(None)]
    assert await replica.event_logs.events(thread, limit=10) == [first, winner]
    assert await replica.event_logs.last_cursor(thread) == 2


async def test_connection_loss_before_commit_keeps_events_projection_and_cursor_atomic(
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    replica: Replica,
    lease: IngestionLease,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.set_attached(thread, protocol_pb2.Attached(session_id="s-1", spec=SPEC), lease=lease)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted())
    await ingestion.record(thread, [first], lease=lease)
    before = await replica.event_logs.feed_state(thread)
    second = event_entry(2, model_changed=event_pb2.ModelChanged(model="test-committed-model"))
    engine = create_async_engine(db_url)

    async def disconnect_before_commit(session: AsyncSession) -> None:
        await notify(session)
        assert await replica.event_logs.events(thread, limit=10) == [first]
        assert await replica.event_logs.last_cursor(thread) == 1
        assert await replica.event_logs.feed_state(thread) == before
        pid = await session.scalar(select(func.pg_backend_pid()))
        async with engine.begin() as connection:
            assert await connection.scalar(select(func.pg_terminate_backend(pid, 5000)))

    try:
        with monkeypatch.context() as patch:
            patch.setattr("agentplane.app.agent_runtime.ingestion.notify", disconnect_before_commit)
            with pytest.raises(DBAPIError):
                await ingestion.record(thread, [second], lease=lease)
        assert await replica.event_logs.events(thread, limit=10) == [first]
        assert await replica.event_logs.last_cursor(thread) == 1
        assert await replica.event_logs.feed_state(thread) == before
        await replica.ingestion.record(thread, [first, second], lease=lease)
        assert await event_logs.events(thread, limit=10) == [first, second]
        assert await event_logs.last_cursor(thread) == 2
        view = await store.get_thread(thread)
        assert view is not None
        assert view.model == "test-committed-model"
    finally:
        await engine.dispose()


async def test_rejected_entry_inside_a_batch_carries_its_cursor(
    event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-invalid-origin", SPEC)
    rejected = event_entry(2, harness_started=event_pb2.HarnessStarted())
    rejected.origin.sequence = 3
    with pytest.raises(EventReplicationError, match="invalid runner origin at cursor 2") as raised:
        await ingestion.record(
            thread,
            [
                event_entry(1, harness_started=event_pb2.HarnessStarted()),
                rejected,
                event_entry(3, harness_started=event_pb2.HarnessStarted()),
            ],
            lease=lease,
        )
    assert raised.value.cursor == 2
    assert await event_logs.last_cursor(thread) == 0


async def test_record_rechecks_expiry_after_waiting_for_the_lease_row(
    event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease, db_url: str
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    engine = create_async_engine(db_url)
    write: asyncio.Task[None] | None = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                select(SandboxIngestion).where(SandboxIngestion.sandbox == lease.sandbox).with_for_update()
            )
            write = asyncio.create_task(
                ingestion.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
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
        assert await event_logs.last_cursor(thread) == 0
    finally:
        if write is not None and not write.done():
            write.cancel()
            await asyncio.gather(write, return_exceptions=True)
        await engine.dispose()


async def test_concurrent_replicas_choose_one_ingester(ingestion: Ingestion, replica: Replica) -> None:
    first, second = await asyncio.gather(
        ingestion.acquire("test-racing-sandbox", timedelta(minutes=1)),
        replica.ingestion.acquire("test-racing-sandbox", timedelta(minutes=1)),
    )
    assert (first is None) != (second is None)


async def test_only_current_lease_can_write_or_renew(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease, db_url: str
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    assert await replica.ingestion.acquire("sb-1", timedelta(minutes=1)) is None
    assert await ingestion.renew(lease, timedelta(minutes=2))
    # Use database time without sleeps: make the first owner stale while retaining its token.
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                update(SandboxIngestion)
                .where(SandboxIngestion.sandbox == "sb-1")
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        assert not await ingestion.renew(lease, timedelta(minutes=1))
        successor = await replica.ingestion.acquire("sb-1", timedelta(minutes=1))
        assert successor is not None
        assert successor.token != lease.token
        await ingestion.release(lease)
        assert await replica.ingestion.renew(successor, timedelta(minutes=1))
        with pytest.raises(IngestionLeaseLostError):
            await ingestion.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
        await replica.ingestion.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=successor)
        wrong_thread = await event_logs.open("sb-2", "s-2", SPEC)
        with pytest.raises(IngestionLeaseLostError):
            await replica.ingestion.record(
                wrong_thread, [event_entry(2, harness_lost=event_pb2.HarnessLost())], lease=successor
            )
        assert await event_logs.last_cursor(wrong_thread) == 0
        await replica.ingestion.release(successor)
        assert await ingestion.acquire("sb-1", timedelta(minutes=1)) is not None
    finally:
        await engine.dispose()


async def test_feed_attachment_and_terminal_state_survive_the_owner(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    assert await replica.event_logs.feed_state(thread) is None
    attached = protocol_pb2.Attached(session_id="s-1", spec=SPEC, last_cursor=7)
    await ingestion.set_attached(thread, attached, lease=lease)
    snapshot = await replica.event_logs.feed_state(thread)
    assert snapshot is not None
    assert snapshot.attached == attached
    assert snapshot.end is None
    await ingestion.end_feed(thread, lease=lease, error=None)
    ended = await replica.event_logs.feed_state(thread)
    assert ended is not None
    assert isinstance(ended.end, FeedEnd)
    await ingestion.set_attached(thread, attached, lease=lease)
    await ingestion.end_feed(thread, lease=lease, error="test runner refused attachment")
    failed = await replica.event_logs.feed_state(thread)
    assert failed is not None
    assert failed.end == FeedError("test runner refused attachment")
    await ingestion.release(lease)
    with pytest.raises(IngestionLeaseLostError):
        await ingestion.set_attached(thread, attached, lease=lease)
    with pytest.raises(IngestionLeaseLostError):
        await ingestion.end_feed(thread, lease=lease, error=None)


async def test_ingested_events_project_the_durable_attachment_without_replay_regression(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    attached = protocol_pb2.Attached(session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_STOPPED)
    await ingestion.set_attached(thread, attached, lease=lease)
    await ingestion.end_feed(thread, lease=lease, error=None)
    await ingestion.record(
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
    running = await replica.event_logs.feed_state(thread)
    assert running is not None
    assert running.end is None
    assert running.attached.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
    assert running.attached.active_turn_id == "test-turn"
    assert running.attached.last_cursor == 3
    assert running.attached.spec == SPEC
    await ingestion.record(
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
    await ingestion.end_feed(thread, lease=lease, error=None)
    stopped = await replica.event_logs.feed_state(thread)
    assert stopped is not None
    assert isinstance(stopped.end, FeedEnd)
    assert stopped.attached.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    assert stopped.attached.active_turn_id == ""
    assert stopped.attached.spec.model == "test-next-model"
    assert stopped.attached.spec.reasoning_effort == SPEC.reasoning_effort
    assert stopped.attached.last_cursor == 8
    view = await replica.store.get_thread(thread)
    assert view is not None
    assert view.model == "test-next-model"
    with pytest.raises(EventReplicationError, match="conflicting runner entry"):
        await ingestion.record(thread, [event_entry(8, harness_started=event_pb2.HarnessStarted())], lease=lease)
    assert await replica.event_logs.feed_state(thread) == stopped
    with pytest.raises(ValueError, match="older"):
        await ingestion.set_attached(thread, attached, lease=lease)
    assert await replica.event_logs.feed_state(thread) == stopped
    await ingestion.record(
        thread, [event_entry(9, harness_started=event_pb2.HarnessStarted(resumed=True))], lease=lease
    )
    resumed = await replica.event_logs.feed_state(thread)
    assert resumed is not None
    assert resumed.end is None
    assert resumed.attached.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
    await ingestion.record(thread, [event_entry(10, harness_lost=event_pb2.HarnessLost())], lease=lease)
    lost = await replica.event_logs.feed_state(thread)
    assert lost is not None
    assert lost.attached.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    assert lost.attached.last_cursor == 10


async def test_historical_catchup_does_not_rewind_an_attachment_snapshot(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_STOPPED, last_cursor=2
    )
    await ingestion.set_attached(thread, attached, lease=lease)
    await ingestion.end_feed(thread, lease=lease, error=None)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted())], lease=lease)
    caught_up = await replica.event_logs.feed_state(thread)
    assert caught_up is not None
    assert caught_up.attached == attached
    assert isinstance(caught_up.end, FeedEnd)
    assert await event_logs.last_cursor(thread) == 1


if __name__ == "__main__":
    pytest_bazel.main()
