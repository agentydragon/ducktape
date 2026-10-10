"""Service-history projection without app raw copies or runner contact."""

from datetime import UTC
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.testing.thread_test_support import SPEC, event_entry
from agentplane.app.threads.events.event_log import EventLogStore, EventReplicationError, FeedEnd
from agentplane.app.threads.events.ingestion_lease import IngestionLease, IngestionLeaseLostError
from agentplane.app.threads.history_handoff import fence_raw_ingestion
from agentplane.app.threads.history_projector import HistoryProjector
from agentplane.app.threads.ingestion import Ingester, Ingestion
from agentplane.app.threads.models import Event, EventLog, FeedState, ThreadCheckpoint, ThreadEntity
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.views import ThreadViewState
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.client import SandboxServiceClient

# gazelle:include_dep @pypi//protobuf


async def test_projection_resumes_existing_checkpoint_without_copying_raw_events(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    second = event_entry(2, turn_started=event_pb2.TurnStarted(turn_id="t1"))
    await ingestion.record(thread, [first], lease=lease)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.side_effect = [
        protocol_pb2.ReadSessionEventsResponse(last_cursor=2, entries=[second]),
        protocol_pb2.ReadSessionEventsResponse(last_cursor=2),
    ]
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 2
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 2
    assert reader.read_session_events.call_args.kwargs["after_cursor"] == 2
    assert await event_logs.last_cursor(thread) == 1
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 2


async def test_failed_projection_can_retry_without_advancing_checkpoint(
    engine: AsyncEngine, event_logs: EventLogStore, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    entry = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=1, entries=[entry])
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    with (
        patch("agentplane.app.threads.history_projector.record_thread_fold", side_effect=ValueError("fold failed")),
        pytest.raises(ValueError, match="fold failed"),
    ):
        await projector.project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) is None
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 1
    assert await event_logs.last_cursor(thread) == 0


async def test_expired_owner_cannot_project(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(
        last_cursor=1, entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))]
    )
    await ingestion.release(lease)
    with pytest.raises(IngestionLeaseLostError):
        await HistoryProjector(engine, cast(SandboxServiceClient, reader)).project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) is None


@pytest.mark.parametrize("case", ["gap", "source_sequence", "beyond_watermark"])
async def test_invalid_history_does_not_advance_projection(
    engine: AsyncEngine, event_logs: EventLogStore, lease: IngestionLease, case: str
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    entry = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    if case == "source_sequence":
        entry.origin.sequence = 9
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(
        last_cursor=0 if case == "beyond_watermark" else 1, entries=[] if case == "gap" else [entry]
    )
    with pytest.raises(EventReplicationError):
        await HistoryProjector(engine, cast(SandboxServiceClient, reader)).project_batch(thread, lease=lease)
    assert await event_logs.last_cursor(thread) == 0


async def test_projection_requires_fence_and_final_raw_coverage(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    reader = AsyncMock(spec=SandboxServiceClient)
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    with pytest.raises(EventReplicationError, match="must be fenced"):
        await projector.project_batch(thread, lease=lease)
    reader.read_session_events.assert_not_awaited()
    await ingestion.record(
        thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))], lease=lease
    )
    assert await fence_raw_ingestion(engine, thread) == 1
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=0)
    with pytest.raises(ConnectionError, match="final raw cursor"):
        await projector.project_batch(thread, lease=lease)


async def test_supervisor_resumes_fenced_deleted_sandbox_without_runner_contact(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion
) -> None:
    thread = await event_logs.open("deleted-sandbox", "s-1", SPEC)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.side_effect = [
        protocol_pb2.ReadSessionEventsResponse(
            last_cursor=1, entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))]
        ),
        protocol_pb2.ReadSessionEventsResponse(last_cursor=1),
    ]
    runners = Mock(spec=SandboxSessions)
    runners.running.return_value = set()
    for _ in range(2):
        coordinator = Ingester(
            runners=cast(SandboxSessions, runners),
            event_logs=event_logs,
            ingestion=ingestion,
            history_projector=HistoryProjector(engine, cast(SandboxServiceClient, reader)),
        )
        try:
            await coordinator.reconcile()
        finally:
            await coordinator.close()
    assert reader.read_session_events.call_args_list[0].kwargs["after_cursor"] == 0
    assert reader.read_session_events.call_args_list[1].kwargs["after_cursor"] == 1
    runners.client.assert_not_called()
    assert await event_logs.last_cursor(thread) == 0
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 1


async def test_projection_updates_activity_atomically_and_ignores_tool_output(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "activity", SPEC)
    first = [
        event_entry(1, turn_started=event_pb2.TurnStarted(turn_id="turn", model=SPEC.model)),
        event_entry(2, item_started=event_pb2.ItemStarted(item_id="call", kind=event_pb2.ITEM_KIND_TOOL_CALL)),
    ]
    await ingestion.record(thread, first, lease=lease)
    await fence_raw_ingestion(engine, thread)
    activity = event_entry(3, tool_arguments=event_pb2.ToolArguments(item_id="call", arguments_json='{"x":"y"}'))
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=3, entries=[activity])
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    # Fail after the fold and activity write: both must roll back together.
    with (
        patch("agentplane.app.threads.history_projector.notify", side_effect=RuntimeError("commit interrupted")),
        pytest.raises(RuntimeError, match="commit interrupted"),
    ):
        await projector.project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 2
        assert await session.scalar(select(EventLog.last_model_activity_at)) == first[-1].event.at.ToDatetime(
            tzinfo=UTC
        )
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 3
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(
        last_cursor=4,
        entries=[event_entry(4, tool_output_delta=event_pb2.ToolOutputDelta(item_id="call", text="still running"))],
    )
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 4
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=4)
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 4
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(EventLog.last_model_activity_at)) == activity.event.at.ToDatetime(tzinfo=UTC)
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 4
        assert await session.scalar(select(func.count()).select_from(Event)) == 2


async def test_thread_metadata_survives_handoff_and_projection_retry(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "summary", SPEC)
    sibling = await event_logs.open("sb-1", "unfenced", SPEC)
    first = [
        event_entry(1, turn_started=event_pb2.TurnStarted(turn_id="one", model=SPEC.model)),
        event_entry(2, turn_completed=event_pb2.TurnCompleted(turn_id="one", status=event_pb2.TURN_STATUS_COMPLETED)),
    ]
    await ingestion.record(thread, first, lease=lease)
    await ingestion.record(sibling, first, lease=lease)
    store = ThreadStore(engine)
    before = await store.get_thread(thread)
    assert before is not None
    await fence_raw_ingestion(engine, thread)
    assert await store.get_thread(thread) == before
    assert (await store.list_threads())[0].last_cursor == 2
    later = [
        event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="two", model=SPEC.model)),
        event_entry(4, turn_completed=event_pb2.TurnCompleted(turn_id="two", status=event_pb2.TURN_STATUS_INTERRUPTED)),
    ]
    # Last-event time is max timestamp, while last-turn status is cursor ordered.
    for entry in later:
        entry.event.at.CopyFrom(first[0].event.at)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=4, entries=later)
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    with (
        patch("agentplane.app.threads.history_projector.notify", side_effect=RuntimeError("interrupted")),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        await projector.project_batch(thread, lease=lease)
    assert await store.get_thread(thread) == before
    await projector.project_batch(thread, lease=lease)
    after = await store.get_thread(thread)
    assert after is not None
    assert after.last_cursor == 4
    assert after.last_event_at == before.last_event_at
    assert after.last_turn_status == "TURN_STATUS_INTERRUPTED"
    views = {view.id: view for view in await store.list_threads()}
    assert views[thread] == after
    assert views[sibling].last_cursor == 2
    assert views[sibling].last_turn_status == "TURN_STATUS_COMPLETED"
    renamed = await store.rename(thread, "retained")
    assert renamed.last_cursor == 4
    assert renamed.last_turn_status == after.last_turn_status
    assert await event_logs.last_cursor(thread) == 2


async def test_fenced_lifecycle_uses_covered_snapshot_and_preserves_legacy_feed(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    initial = runner_pb2.Attached(session_id="s-1", spec=SPEC, harness_state=runner_pb2.HARNESS_STATE_RUNNING)
    await ingestion.set_attached(thread, initial, lease=lease)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    page = protocol_pb2.ReadSessionEventsResponse(
        last_cursor=2,
        entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(pid=7))],
        feed_state=protocol_pb2.SessionFeedState(
            attached=runner_pb2.Attached(
                session_id="s-1", spec=SPEC, last_cursor=2, harness_state=runner_pb2.HARNESS_STATE_STOPPED
            ),
            ended=True,
        ),
    )
    reader.read_session_events.return_value = page
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    await projector.project_batch(thread, lease=lease)
    snapshot = await event_logs.feed_state(thread)
    assert snapshot is not None
    assert snapshot.attached.last_cursor == 1
    assert snapshot.end is None
    del page.entries[:]
    page.entries.append(event_entry(2, harness_exited=event_pb2.HarnessExited()))
    await projector.project_batch(thread, lease=lease)
    snapshot = await event_logs.feed_state(thread)
    assert snapshot is not None
    assert isinstance(snapshot.end, FeedEnd)
    assert snapshot.attached.last_cursor == 2
    views = {view.id: view for view in await ThreadStore(engine).list_threads()}
    assert views[thread].feed_status == "ended"
    assert views[thread].harness_state == "HARNESS_STATE_STOPPED"
    async with async_sessionmaker(engine)() as session:
        old = await session.get(FeedState, thread)
        assert old is not None
        assert old.end is None
        assert old.attached.get("lastCursor", "0") == "0"
    # A confirmed resume must not write frozen FeedState or be undone by stale EOF.
    await event_logs.resume_pending(thread)
    del page.entries[:]
    await projector.project_batch(thread, lease=lease)
    snapshot = await event_logs.feed_state(thread)
    assert snapshot is not None
    assert snapshot.end is None
    page.last_cursor = 3
    page.entries.append(event_entry(3, harness_started=event_pb2.HarnessStarted(resumed=True, pid=8)))
    await projector.project_batch(thread, lease=lease)
    snapshot = await event_logs.feed_state(thread)
    assert snapshot is not None
    assert snapshot.attached.last_cursor == 3
    assert snapshot.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert snapshot.end is None


async def test_empty_page_adopts_eof_and_rolls_back_invalid_snapshot(
    engine: AsyncEngine, event_logs: EventLogStore, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "empty", SPEC)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    page = protocol_pb2.ReadSessionEventsResponse(
        last_cursor=0,
        feed_state=protocol_pb2.SessionFeedState(
            attached=runner_pb2.Attached(session_id="empty", spec=SPEC, last_cursor=1), ended=True
        ),
    )
    reader.read_session_events.return_value = page
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    with pytest.raises(EventReplicationError, match="outside"):
        await projector.project_batch(thread, lease=lease)
    assert await event_logs.feed_state(thread) is None
    page.feed_state.attached.last_cursor = 0
    await projector.project_batch(thread, lease=lease)
    snapshot = await event_logs.feed_state(thread)
    assert snapshot is not None
    assert isinstance(snapshot.end, FeedEnd)
    page.feed_state.ended = False
    await projector.project_batch(thread, lease=lease)
    snapshot = await event_logs.feed_state(thread)
    assert snapshot is not None
    assert isinstance(snapshot.end, FeedEnd)


async def test_deleted_history_keeps_seeded_terminal_state_without_service_snapshot(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "gone", SPEC)
    await ingestion.set_attached(thread, runner_pb2.Attached(session_id="gone", spec=SPEC), lease=lease)
    await ingestion.end_feed(thread, lease=lease, error=None)
    before = await event_logs.feed_state(thread)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=0)
    await HistoryProjector(engine, cast(SandboxServiceClient, reader)).project_batch(thread, lease=lease)
    assert await event_logs.feed_state(thread) == before
    assert (await ThreadStore(engine).rename(thread, "archive")).feed_status == "ended"


async def test_projection_failure_retains_checkpoint_not_eof_and_recovers(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "projection-failure", SPEC)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=7))], lease=lease)
    await fence_raw_ingestion(engine, thread)
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_events.side_effect = ConnectionError("private backend details")
    projector = HistoryProjector(engine, cast(SandboxServiceClient, reader))
    with pytest.raises(ConnectionError):
        await projector.project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 1
        state = await session.scalar(select(ThreadEntity.state).where(ThreadEntity.entity_id == "current"))
        assert state is not None
        assert ThreadViewState.model_validate(state).operational.status == "failed"
        assert "private backend" not in str(state)
    assert await event_logs.feed_state(thread) is None  # failure is not runner EOF
    reader.read_session_events.side_effect = None
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=1)
    await projector.project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        state = await session.scalar(select(ThreadEntity.state).where(ThreadEntity.entity_id == "current"))
        assert state is not None
        assert ThreadViewState.model_validate(state).operational.status == "active"
        assert ThreadViewState.model_validate(state).operational.feed_error is None
    # A delayed failure report cannot overwrite a newer committed prefix.
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(
        last_cursor=2, entries=[event_entry(2, turn_started=event_pb2.TurnStarted(turn_id="turn"))]
    )
    await projector.project_batch(thread, lease=lease)
    await projector._record_failure(thread, lease=lease, after=1, error=ConnectionError())
    async with async_sessionmaker(engine)() as session:
        state = await session.scalar(select(ThreadEntity.state).where(ThreadEntity.entity_id == "current"))
        assert state is not None
        assert ThreadViewState.model_validate(state).operational.status == "active"
    await ingestion.release(lease)
    with pytest.raises(IngestionLeaseLostError):
        await projector._record_failure(thread, lease=lease, after=2, error=ConnectionError())


async def test_discovery_of_new_service_thread_never_starts_legacy_follow(
    engine: AsyncEngine, ingestion: Ingestion
) -> None:
    public_id = uuid4()
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_observations.return_value = protocol_pb2.ReadSessionObservationsResponse(last_cursor=0)
    reader.read_session_events.return_value = protocol_pb2.ReadSessionEventsResponse(last_cursor=0)
    logs = EventLogStore(engine, history_creator=cast(SandboxServiceClient, reader))
    client = AsyncMock()
    client.list_sessions.return_value = [
        runner_pb2.SessionSummary(session_id="unregistered-legacy-session", spec=SPEC),
        runner_pb2.SessionSummary(session_id=str(public_id), spec=SPEC),
    ]
    runners = Mock(spec=SandboxSessions)
    runners.running.return_value = {"sb-1"}
    runners.client.return_value = client
    coordinator = Ingester(
        runners=cast(SandboxSessions, runners),
        event_logs=logs,
        ingestion=ingestion,
        history_projector=HistoryProjector(engine, cast(SandboxServiceClient, reader)),
    )
    try:
        await coordinator.reconcile()
        assert await logs.is_raw_ingestion_fenced(public_id)
        await coordinator.reconcile()
        reader.read_session_events.assert_awaited_once_with(str(public_id), after_cursor=0, limit=128)
        client.follow.assert_not_called()
    finally:
        await coordinator.close()


if __name__ == "__main__":
    pytest_bazel.main()
