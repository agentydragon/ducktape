"""The History Service fold over a migrated history database, read from its own raw log."""

import asyncio
import typing
from datetime import UTC, datetime, timedelta
from uuid import UUID

import grpc
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from grpc_health.v1 import health_pb2, health_pb2_grpc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.changes import ChangeSignal
from agentplane.grpc_serving import add_health, serving
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.fold.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadHistorySummary,
    ThreadNativeLink,
    ThreadPayloadChunk,
)
from agentplane.history_service.fold.views import EntityKind
from agentplane.history_service.fold_runner import FOLD_EPOCH, HEALTH_SERVICE, FoldBacklog, FoldRunner
from agentplane.history_service.storage.store import Store
from agentplane.history_service.testing.delta_spans import claude_text
from agentplane.history_service.testing.sessions import retained_session
from agentplane.proto import event_log_pb2, event_pb2, history_service_pb2, runner_pb2

PAGE_SIZE = 8


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


async def test_folds_retained_sessions_a_page_per_cycle_until_caught_up(engine: AsyncEngine, history: Store) -> None:
    entries = [entry(1, harness_started=event_pb2.HarnessStarted(pid=7)), *turn(2)]
    folded = await retained_session(history, entries)
    empty = await retained_session(history, [])
    folder = FoldRunner(engine=engine, history=history, concurrency=2, page_size=PAGE_SIZE)

    assert await folder.cycle() == FoldBacklog(sessions=2, behind=1, events_behind=len(entries) - PAGE_SIZE)
    assert await folder.cycle() == FoldBacklog(sessions=2, behind=0, events_behind=0)

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


async def test_a_session_is_idle_from_its_harness_start_or_last_turn_end_until_the_next_turn_or_stop(
    engine: AsyncEngine, history: Store
) -> None:
    session_id = await retained_session(history, [])
    folder = FoldRunner(engine=engine, history=history, concurrency=1, page_size=PAGE_SIZE)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def fold_idle_since(*entries: event_log_pb2.EventEntry) -> datetime | None:
        await history.append(session_id, list(entries))
        await folder.cycle()
        async with sessions() as session:
            summary = await session.get(ThreadHistorySummary, session_id)
            assert summary is not None
            return summary.idle_since

    def at(cursor: int) -> datetime:
        return entry(cursor).event.at.ToDatetime(tzinfo=UTC)

    assert await fold_idle_since(entry(1, harness_started=event_pb2.HarnessStarted(pid=7))) == at(1)
    assert await fold_idle_since(entry(2, turn_started=event_pb2.TurnStarted(turn_id="test-turn"))) is None
    assert (
        await fold_idle_since(
            entry(3, item_started=event_pb2.ItemStarted(item_id="test-reply", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT))
        )
        is None
    )
    done = entry(4, turn_completed=event_pb2.TurnCompleted(turn_id="test-turn", status=event_pb2.TURN_STATUS_FAILED))
    assert await fold_idle_since(done) == at(4)
    assert await fold_idle_since(entry(5, text_delta=event_pb2.TextDelta(item_id="test-late", text="x"))) == at(4)
    assert await fold_idle_since(entry(6, harness_exited=event_pb2.HarnessExited())) is None


async def test_a_session_ends_only_once_its_harness_launch_has_an_outcome(engine: AsyncEngine, history: Store) -> None:
    # The runner announces a Session whose setup script runs before its harness starts.
    session_id = await retained_session(
        history,
        [
            entry(1, setup_started=event_pb2.SetupStarted()),
            entry(2, setup_output=event_pb2.SetupOutput(stdout=b"test setup output\n")),
        ],
    )
    announce = runner_pb2.Attached(
        session_id=str(session_id),
        harness_state=runner_pb2.HARNESS_STATE_STOPPED,
        setup_state=runner_pb2.SETUP_STATE_RUNNING,
        launch_outcome=runner_pb2.LAUNCH_OUTCOME_PENDING,
    )
    await history.record_feed_state(session_id, history_service_pb2.SessionFeedState(attached=announce))
    folder = FoldRunner(engine=engine, history=history, concurrency=1, page_size=PAGE_SIZE)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def fold_end(*entries: event_log_pb2.EventEntry) -> dict[str, str] | None:
        if entries:
            await history.append(session_id, list(entries))
        await folder.cycle()
        async with sessions() as session:
            summary = await session.get(ThreadHistorySummary, session_id)
            assert summary is not None
            return summary.end

    assert await fold_end() is None
    # Setup succeeded; the harness is still launching.
    assert await fold_end(entry(3, setup_finished=event_pb2.SetupFinished(exit_code=0))) is None
    assert await fold_end(entry(4, harness_launch_failed=event_pb2.HarnessLaunchFailed(reason="test failure"))) == {}


async def test_run_folds_without_waiting_out_the_interval_while_behind(engine: AsyncEngine, history: Store) -> None:
    entries = turn(1)
    session_id = await retained_session(history, entries)
    folder = FoldRunner(engine=engine, history=history, concurrency=1, page_size=PAGE_SIZE)
    wakeups = create_wakeups(engine.url)
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

    async with wakeups.listener.listen(), asyncio.TaskGroup() as tasks:
        # More than a page behind: an interval wait between pages would outlast the timeout.
        running = tasks.create_task(
            folder.run(interval_s=3600, changes=wakeups.changes[Channel.HISTORY], health=add_health(grpc.aio.server()))
        )
        async with asyncio.timeout(30):
            await caught_up(len(entries))
            await history.append(session_id, [entry(len(entries) + 1, harness_started=event_pb2.HarnessStarted())])
            await caught_up(len(entries) + 1)
        running.cancel()


async def test_replicas_split_sessions_by_lease(engine: AsyncEngine, history: Store) -> None:
    session_id = await retained_session(history, turn(1))
    first = FoldRunner(engine=engine, history=history, concurrency=1, page_size=PAGE_SIZE)
    second = FoldRunner(engine=engine, history=history, concurrency=1, page_size=PAGE_SIZE)

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
    folder = FoldRunner(engine=engine, history=history, concurrency=2, page_size=PAGE_SIZE)

    assert await folder.cycle() == FoldBacklog(sessions=2, behind=0, events_behind=0)

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
    folder = FoldRunner(engine=engine, history=settling, concurrency=1, page_size=PAGE_SIZE)

    await folder.cycle()
    async with async_sessionmaker(engine)() as session:
        evidence, native = await observed_cursors(session, session_id)
        assert ({4, 6} <= evidence, {3, 5} <= native) == (True, True)

    await settling.append(session_id, span[6:])
    await folder.cycle()
    async with async_sessionmaker(engine)() as session:
        evidence, native = await observed_cursors(session, session_id)
        assert not (evidence | native) & set(range(3, 7))
        assert 8 in evidence
        reply = await session.get(ThreadEntity, (session_id, FOLD_EPOCH, EntityKind.ITEM, "msg_test#0"))
        assert reply is not None
        assert not reply.pending


class BrokenFoldStore(Store):
    @typing.override
    async def read_page(
        self, session_id: UUID, *, after_cursor: int = 0, limit: int = 128
    ) -> history_service_pb2.ReadSessionEventsResponse:
        raise TypeError("test: a fold bug")


async def test_an_unretried_error_stops_the_fold_and_reports_it(engine: AsyncEngine) -> None:
    await retained_session(Store(engine), turn(1))
    folder = FoldRunner(engine=engine, history=BrokenFoldStore(engine), concurrency=1, page_size=PAGE_SIZE)
    server = grpc.aio.server()
    health = add_health(server)
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
    ):
        async with asyncio.timeout(30):
            await folder.run(interval_s=3600, changes=ChangeSignal(), health=health)
        checked = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(service=HEALTH_SERVICE))
        whole = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
    assert (checked.status, whole.status) == (
        health_pb2.HealthCheckResponse.NOT_SERVING,
        health_pb2.HealthCheckResponse.SERVING,
    )


if __name__ == "__main__":
    pytest_bazel.main()
