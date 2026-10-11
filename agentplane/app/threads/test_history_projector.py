"""Service-history projection without app raw copies or runner contact."""

import asyncio
from datetime import UTC, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.database_updates import Channel, DatabaseUpdates, notify_threads
from agentplane.app.testing.history import ProjectedHistory as Ingestion, SeededEventLogStore as EventLogStore
from agentplane.app.testing.projection_lease import LeaseFactory
from agentplane.app.testing.retained_history import seed_retained_session
from agentplane.app.testing.thread_test_support import SPEC, event_entry
from agentplane.app.threads import ingestion as ingestion_module
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore as ServiceEventLogStore, ServiceSessionReference
from agentplane.app.threads.history_watch import HistoryWatch
from agentplane.app.threads.ingestion import Ingester
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import AppSummaries, FeedEnd
from agentplane.app.threads.view.content import ContentStore
from agentplane.history_service import protocol_pb2 as history_pb2
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_pb2, runner_pb2, sandbox_service_pb2
from agentplane.thread_fold.models import ThreadCheckpoint, ThreadEntity, ThreadHistorySummary
from agentplane.thread_fold.projection_lease import ProjectionLeaseLostError
from agentplane.thread_fold.projector import HistoryProjector, service_pages
from agentplane.thread_fold.recording import THREAD_FOLD_EPOCH, THREAD_FOLD_PAGE_SIZE, EventReplicationError
from agentplane.thread_fold.views import ThreadViewState

# gazelle:include_dep @pypi//protobuf


async def test_projection_resumes_existing_checkpoint_without_copying_raw_events(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "s-1")
    lease = await lease_for(thread)
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    second = event_entry(2, turn_started=event_pb2.TurnStarted(turn_id="t1"))
    await ingestion.record(thread, [first], lease=lease)
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.side_effect = [
        history_pb2.ReadSessionEventsResponse(last_cursor=2, entries=[second]),
        history_pb2.ReadSessionEventsResponse(last_cursor=2),
    ]
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 2
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 2
    assert reader.read_session_events.call_args.kwargs["after_cursor"] == 2
    assert await event_logs.last_cursor(thread) == 2
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 2


async def test_failed_projection_can_retry_without_advancing_checkpoint(
    engine: AsyncEngine, event_logs: EventLogStore, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "s-1")
    lease = await lease_for(thread)
    entry = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=1, entries=[entry])
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
    with (
        patch("agentplane.thread_fold.projector.record_thread_fold", side_effect=ValueError("fold failed")),
        pytest.raises(ValueError, match="fold failed"),
    ):
        await projector.project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) is None
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 1
    assert await event_logs.last_cursor(thread) == 1


async def test_expired_owner_cannot_project(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "s-1")
    lease = await lease_for(thread)
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(
        last_cursor=1, entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))]
    )
    await ingestion.release(lease)
    with pytest.raises(ProjectionLeaseLostError):
        await HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ).project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) is None


@pytest.mark.parametrize("case", ["gap", "source_sequence", "beyond_watermark"])
async def test_invalid_history_does_not_advance_projection(
    engine: AsyncEngine, event_logs: EventLogStore, lease_for: LeaseFactory, case: str
) -> None:
    thread = await event_logs.open("sb-1", "s-1")
    lease = await lease_for(thread)
    entry = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    if case == "source_sequence":
        entry.origin.sequence = 9
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(
        last_cursor=0 if case == "beyond_watermark" else 1, entries=[] if case == "gap" else [entry]
    )
    with pytest.raises(EventReplicationError):
        await HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ).project_batch(thread, lease=lease)
    assert await event_logs.last_cursor(thread) == 0


async def test_projection_requires_service_coverage_of_checkpoint(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "s-1")
    lease = await lease_for(thread)
    await ingestion.record(
        thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))], lease=lease
    )
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=0)
    with pytest.raises(ConnectionError, match="projection checkpoint"):
        await HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ).project_batch(thread, lease=lease)
    assert await event_logs.last_cursor(thread) == 1


async def test_supervisor_resumes_deleted_sandbox_without_runner_contact(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion
) -> None:
    thread = await event_logs.open("deleted-sandbox", "s-1")
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.side_effect = [
        history_pb2.ReadSessionEventsResponse(
            last_cursor=1, entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))]
        ),
        history_pb2.ReadSessionEventsResponse(last_cursor=1),
    ]
    runners = Mock(spec=SandboxSessions)
    runners.running.return_value = set()
    for _ in range(2):
        coordinator = Ingester(
            runners=cast(SandboxSessions, runners),
            event_logs=event_logs,
            ingestion=ingestion,
            history_projector=HistoryProjector(
                engine,
                service_pages(cast(HistoryServiceClient, reader)),
                projection_epoch=THREAD_FOLD_EPOCH,
                page_size=THREAD_FOLD_PAGE_SIZE,
                notify=notify_threads,
            ),
        )
        try:
            await coordinator.reconcile()
        finally:
            await coordinator.close()
    assert reader.read_session_events.call_args_list[0].kwargs["after_cursor"] == 0
    assert reader.read_session_events.call_args_list[1].kwargs["after_cursor"] == 1
    runners.client.assert_not_called()
    assert await event_logs.last_cursor(thread) == 1
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 1


async def test_raw_history_watch_wakes_multi_page_projection_without_polling(
    engine: AsyncEngine,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    runners: SandboxSessions,
    database_updates: DatabaseUpdates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingestion_module, "RECONCILE_S", 3600)
    thread = await event_logs.open("sb-1", "watch-wakeup")

    class ObservedIngester(Ingester):
        def __init__(self) -> None:
            super().__init__(
                runners=runners, event_logs=event_logs, ingestion=ingestion, history_projector=ingestion.projector
            )
            self.reconciled = asyncio.Event()

        async def reconcile(self) -> None:
            await super().reconcile()
            self.reconciled.set()

    coordinator = ObservedIngester()
    async with event_logs.reader.new_client() as watch_client:
        history_watch = HistoryWatch(
            watch_client,
            lambda: watch_client.watch_session_history(timeout_s=300),
            coordinator.wake,
            retry_interval_s=1,
        )
        watch_task = asyncio.create_task(history_watch.run())
        try:
            await coordinator.start()
            await asyncio.wait_for(coordinator.reconciled.wait(), 10)
            await asyncio.wait_for(event_logs.peer.history_watch_opened.wait(), 10)
            entries = [
                event_entry(cursor, harness_stderr=event_pb2.HarnessStderr(text=f"line {cursor}"))
                for cursor in range(1, 257)
            ]
            event_logs.peer.publish(thread, entries)

            changed = asyncio.Event()
            changes = database_updates.changes[Channel.THREADS]
            with changes.subscribe(changed):
                while True:
                    changed.clear()
                    async with async_sessionmaker(engine)() as session:
                        through = await session.scalar(
                            select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread)
                        )
                    if through == len(entries):
                        break
                    await asyncio.wait_for(changed.wait(), 10)

                # The initial stream invalidation and 256-entry backlog have been consumed. A new
                # commit must wake an idle coordinator without relying on the normal interval.
                event_logs.peer.publish(
                    thread, [event_entry(257, harness_stderr=event_pb2.HarnessStderr(text="after backlog"))]
                )
                while True:
                    changed.clear()
                    async with async_sessionmaker(engine)() as session:
                        through = await session.scalar(
                            select(ThreadCheckpoint.through_cursor).where(ThreadCheckpoint.thread_id == thread)
                        )
                    if through == 257:
                        break
                    await asyncio.wait_for(changed.wait(), 10)
        finally:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
            await coordinator.close()


async def test_projection_updates_activity_atomically_and_ignores_tool_output(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "activity")
    lease = await lease_for(thread)
    first = [
        event_entry(1, turn_started=event_pb2.TurnStarted(turn_id="turn", model=SPEC.model)),
        event_entry(2, item_started=event_pb2.ItemStarted(item_id="call", kind=event_pb2.ITEM_KIND_TOOL_CALL)),
    ]
    await ingestion.record(thread, first, lease=lease)
    activity = event_entry(3, tool_arguments=event_pb2.ToolArguments(item_id="call", arguments_json='{"x":"y"}'))
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=3, entries=[activity])
    interrupted = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=AsyncMock(side_effect=RuntimeError("commit interrupted")),
    )
    # Fail after the fold and activity write: both must roll back together.
    with pytest.raises(RuntimeError, match="commit interrupted"):
        await interrupted.project_batch(thread, lease=lease)
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 2
        assert await session.scalar(select(ThreadHistorySummary.last_model_activity_at)) == first[
            -1
        ].event.at.ToDatetime(tzinfo=UTC)
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 3
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(
        last_cursor=4,
        entries=[event_entry(4, tool_output_delta=event_pb2.ToolOutputDelta(item_id="call", text="still running"))],
    )
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 4
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=4)
    assert (await projector.project_batch(thread, lease=lease)).through_cursor == 4
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(
            select(ThreadHistorySummary.last_model_activity_at)
        ) == activity.event.at.ToDatetime(tzinfo=UTC)
        assert await session.scalar(select(ThreadCheckpoint.through_cursor)) == 4


async def test_thread_metadata_survives_projection_retry(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "summary")
    lease = await lease_for(thread)
    sibling = await event_logs.open("sb-1", "sibling")
    sibling_lease = await lease_for(sibling)
    first = [
        event_entry(1, turn_started=event_pb2.TurnStarted(turn_id="one", model=SPEC.model)),
        event_entry(2, turn_completed=event_pb2.TurnCompleted(turn_id="one", status=event_pb2.TURN_STATUS_COMPLETED)),
    ]
    await ingestion.record(thread, first, lease=lease)
    await ingestion.record(sibling, first, lease=sibling_lease)
    store = ThreadStore(engine, AppSummaries(engine))
    before = await store.get_thread(thread)
    assert before is not None
    assert (await store.list_threads())[0].last_cursor == 2
    later = [
        event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="two", model=SPEC.model)),
        event_entry(4, turn_completed=event_pb2.TurnCompleted(turn_id="two", status=event_pb2.TURN_STATUS_INTERRUPTED)),
    ]
    # Last-event time is max timestamp, while last-turn status is cursor ordered.
    for entry in later:
        entry.event.at.CopyFrom(first[0].event.at)
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=4, entries=later)
    interrupted = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=AsyncMock(side_effect=RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await interrupted.project_batch(thread, lease=lease)
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
    assert await store.get_thread(thread) == before
    await projector.project_batch(thread, lease=lease)
    after = await store.get_thread(thread)
    assert after is not None
    assert after.last_cursor == 4
    service_logs = ServiceEventLogStore(
        engine, summaries=AppSummaries(engine), history_reader=cast(HistoryServiceClient, reader)
    )
    assert await service_logs.last_cursor(thread) == 4
    assert after.last_event_at == before.last_event_at
    assert after.last_turn_status == "TURN_STATUS_INTERRUPTED"
    views = {view.id: view for view in await store.list_threads()}
    assert views[thread] == after
    assert views[sibling].last_cursor == 2
    assert views[sibling].last_turn_status == "TURN_STATUS_COMPLETED"
    renamed = await store.rename(thread, "retained")
    assert renamed.last_cursor == 4
    assert renamed.last_turn_status == after.last_turn_status
    assert await event_logs.last_cursor(thread) == 4


async def test_lifecycle_uses_only_covered_service_snapshot(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "s-1")
    lease = await lease_for(thread)
    initial = runner_pb2.Attached(session_id="s-1", spec=SPEC, harness_state=runner_pb2.HARNESS_STATE_RUNNING)
    await ingestion.set_attached(thread, initial, lease=lease)
    reader = AsyncMock(spec=HistoryServiceClient)
    page = history_pb2.ReadSessionEventsResponse(
        last_cursor=2,
        entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(pid=7))],
        feed_state=history_pb2.SessionFeedState(
            attached=runner_pb2.Attached(
                session_id="s-1", spec=SPEC, last_cursor=2, harness_state=runner_pb2.HARNESS_STATE_STOPPED
            ),
            ended=True,
        ),
    )
    reader.read_session_events.return_value = page
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
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
    views = {view.id: view for view in await ThreadStore(engine, AppSummaries(engine)).list_threads()}
    assert views[thread].feed_status == "ended"
    assert views[thread].harness_state == "HARNESS_STATE_STOPPED"

    # A confirmed resume must not be undone by stale EOF from the service.
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


async def test_unchanged_idle_projection_does_not_write_or_notify(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "idle")
    lease = await lease_for(thread)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=7))], lease=lease)
    reader = AsyncMock(spec=HistoryServiceClient)
    page = history_pb2.ReadSessionEventsResponse(
        last_cursor=1,
        feed_state=history_pb2.SessionFeedState(
            attached=runner_pb2.Attached(session_id="idle", spec=SPEC, last_cursor=1)
        ),
    )
    reader.read_session_events.return_value = page
    notify = AsyncMock()
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify,
    )
    await projector.project_batch(thread, lease=lease)
    notify.reset_mock()

    statements = Mock()
    event.listen(engine.sync_engine, "before_cursor_execute", statements)
    try:
        await projector.project_batch(thread, lease=lease)
        notify.assert_not_awaited()
        assert not any(
            call.args[2].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for call in statements.call_args_list
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", statements)

    # A lifecycle-only change must still reach subscribers, exactly once.
    page.feed_state.ended = True
    await projector.project_batch(thread, lease=lease)
    notify.assert_awaited_once()
    notify.reset_mock()
    await projector.project_batch(thread, lease=lease)
    notify.assert_not_awaited()


async def test_empty_page_adopts_eof_and_rolls_back_invalid_snapshot(
    engine: AsyncEngine, event_logs: EventLogStore, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "empty")
    lease = await lease_for(thread)
    reader = AsyncMock(spec=HistoryServiceClient)
    page = history_pb2.ReadSessionEventsResponse(
        last_cursor=0,
        feed_state=history_pb2.SessionFeedState(
            attached=runner_pb2.Attached(session_id="empty", spec=SPEC, last_cursor=1), ended=True
        ),
    )
    reader.read_session_events.return_value = page
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
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
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "gone")
    lease = await lease_for(thread)
    await ingestion.set_attached(thread, runner_pb2.Attached(session_id="gone", spec=SPEC), lease=lease)
    await ingestion.end_feed(thread, lease=lease, error=None)
    before = await event_logs.feed_state(thread)
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=0)
    await HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    ).project_batch(thread, lease=lease)
    assert await event_logs.feed_state(thread) == before
    assert (await ThreadStore(engine, AppSummaries(engine)).rename(thread, "archive")).feed_status == "ended"


async def test_projection_failure_retains_checkpoint_not_eof_and_recovers(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    thread = await event_logs.open("sb-1", "projection-failure")
    lease = await lease_for(thread)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=7))], lease=lease)
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.side_effect = ConnectionError("private backend details")
    projector = HistoryProjector(
        engine,
        service_pages(cast(HistoryServiceClient, reader)),
        projection_epoch=THREAD_FOLD_EPOCH,
        page_size=THREAD_FOLD_PAGE_SIZE,
        notify=notify_threads,
    )
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
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=1)
    await projector.project_batch(thread, lease=lease)
    async with async_sessionmaker(engine)() as session:
        state = await session.scalar(select(ThreadEntity.state).where(ThreadEntity.entity_id == "current"))
        assert state is not None
        assert ThreadViewState.model_validate(state).operational.status == "active"
        assert ThreadViewState.model_validate(state).operational.feed_error is None
    # A delayed failure report cannot overwrite a newer committed prefix.
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(
        last_cursor=2, entries=[event_entry(2, turn_started=event_pb2.TurnStarted(turn_id="turn"))]
    )
    await projector.project_batch(thread, lease=lease)
    await projector._record_failure(thread, lease=lease, after=1, error=ConnectionError())
    async with async_sessionmaker(engine)() as session:
        state = await session.scalar(select(ThreadEntity.state).where(ThreadEntity.entity_id == "current"))
        assert state is not None
        assert ThreadViewState.model_validate(state).operational.status == "active"
    await ingestion.release(lease)
    with pytest.raises(ProjectionLeaseLostError):
        await projector._record_failure(thread, lease=lease, after=2, error=ConnectionError())


async def test_discovery_of_new_service_thread_never_starts_legacy_follow(
    engine: AsyncEngine, ingestion: Ingestion
) -> None:
    public_id = uuid4()
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.return_value = history_pb2.ReadSessionObservationsResponse(last_cursor=0)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=0)
    logs = ServiceEventLogStore(
        engine, summaries=AppSummaries(engine), history_reader=cast(HistoryServiceClient, reader)
    )
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
        history_projector=HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ),
    )
    try:
        await coordinator.reconcile()
        assert public_id in await logs.projection_sessions()
        await coordinator.reconcile()
        reader.read_session_events.assert_awaited_once_with(str(public_id), after_cursor=0, limit=THREAD_FOLD_PAGE_SIZE)
        client.follow.assert_not_called()
    finally:
        await coordinator.close()


async def test_discovery_racing_open_agrees_on_the_service_id(engine: AsyncEngine, ingestion: Ingestion) -> None:
    """Discovery lists the runner session before CreateSession's reply reaches the app."""
    public_id = uuid4()
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.return_value = history_pb2.ReadSessionObservationsResponse(last_cursor=0)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(last_cursor=0)
    logs = ServiceEventLogStore(
        engine, summaries=AppSummaries(engine), history_reader=cast(HistoryServiceClient, reader)
    )
    client = AsyncMock()
    client.list_sessions.return_value = [runner_pb2.SessionSummary(session_id=str(public_id), spec=SPEC)]
    client.create.return_value = sandbox_service_pb2.CreateSessionResponse(
        session_id=str(public_id), attached=runner_pb2.Attached(session_id=str(public_id), spec=SPEC)
    )
    runners = Mock(spec=SandboxSessions)
    runners.running.return_value = {"sb-1"}
    runners.client.return_value = client
    coordinator = Ingester(
        runners=cast(SandboxSessions, runners),
        event_logs=logs,
        ingestion=ingestion,
        history_projector=HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ),
    )
    bridge = RunnerBridge(
        runners=cast(SandboxSessions, runners),
        event_logs=logs,
        content=cast(ContentStore, Mock(spec=ContentStore)),
        ingester=cast(Ingester, AsyncMock(spec=Ingester)),
    )
    try:
        await coordinator.reconcile()
        assert await logs.service_session(public_id) == ServiceSessionReference("sb-1", str(public_id))
        attached = await bridge.create_session("sb-1", public_id, {})
        assert attached.session_id == str(public_id)
        assert await logs.service_session(public_id) == ServiceSessionReference("sb-1", str(public_id))
    finally:
        await coordinator.close()


async def test_sessions_without_projection_metadata_are_not_scheduled(
    engine: AsyncEngine, ingestion: Ingestion
) -> None:
    thread = await seed_retained_session(engine)
    reader = AsyncMock(spec=HistoryServiceClient)
    logs = ServiceEventLogStore(
        engine, summaries=AppSummaries(engine), history_reader=cast(HistoryServiceClient, reader)
    )
    assert thread not in await logs.projection_sessions()
    runners = Mock(spec=SandboxSessions)
    runners.running.return_value = set()
    coordinator = Ingester(
        runners=cast(SandboxSessions, runners),
        event_logs=logs,
        ingestion=ingestion,
        history_projector=HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ),
    )
    try:
        await coordinator.reconcile()
        reader.read_session_events.assert_not_awaited()
        runners.client.assert_not_called()
    finally:
        await coordinator.close()


async def test_runtime_feed_metadata_requires_projection_summary(engine: AsyncEngine) -> None:
    thread = await seed_retained_session(engine)
    attached = {"sessionId": "s-retained"}
    reader = AsyncMock(spec=HistoryServiceClient)
    runtime = ServiceEventLogStore(
        engine, summaries=AppSummaries(engine), history_reader=cast(HistoryServiceClient, reader)
    )
    assert await runtime.feed_state(thread) is None
    await runtime.resume_pending(thread)
    with pytest.raises(ValueError, match="missing service projection metadata"):
        await ThreadStore(engine, AppSummaries(engine)).get_thread(thread)
    async with async_sessionmaker(engine).begin() as session:
        session.add(ThreadHistorySummary(thread_id=thread, attached=attached, end={}))
    snapshot = await runtime.feed_state(thread)
    assert snapshot is not None
    assert isinstance(snapshot.end, FeedEnd)
    await runtime.resume_pending(thread)
    current = await runtime.feed_state(thread)
    assert current is not None
    assert current.end is None


async def test_coordinator_projects_sibling_owned_by_another_replica_independently(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease_for: LeaseFactory
) -> None:
    first = await event_logs.open("same-sandbox", "first")
    second = await event_logs.open("same-sandbox", "second")
    other_replica_lease = await lease_for(first)
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_events.return_value = history_pb2.ReadSessionEventsResponse(
        last_cursor=1, entries=[event_entry(1, harness_started=event_pb2.HarnessStarted(pid=7))]
    )
    runners = Mock(spec=SandboxSessions)
    runners.running.return_value = set()
    coordinator = Ingester(
        runners=cast(SandboxSessions, runners),
        event_logs=event_logs,
        ingestion=ingestion,
        history_projector=HistoryProjector(
            engine,
            service_pages(cast(HistoryServiceClient, reader)),
            projection_epoch=THREAD_FOLD_EPOCH,
            page_size=THREAD_FOLD_PAGE_SIZE,
            notify=notify_threads,
        ),
    )
    try:
        await coordinator.reconcile()
        assert await event_logs.last_cursor(first) == 0
        assert await event_logs.last_cursor(second) == 1
        assert await ingestion.renew(other_replica_lease, timedelta(minutes=1))
        assert await ingestion.acquire(second, timedelta(minutes=1)) is None
        runners.client.assert_not_called()
    finally:
        await coordinator.close()
        await ingestion.release(other_replica_lease)
    assert await ingestion.acquire(second, timedelta(minutes=1)) is not None


if __name__ == "__main__":
    pytest_bazel.main()
