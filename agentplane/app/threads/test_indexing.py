"""The store's access paths under a large history: what a read touches must not grow with it."""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Sequence
from datetime import UTC, timedelta
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.testing.history import ProjectedHistory as Ingestion, SeededEventLogStore as EventLogStore
from agentplane.app.testing.projection_lease import LeaseFactory
from agentplane.app.testing.thread_test_support import event_entry
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.proto import command_pb2, event_log_pb2, event_pb2
from agentplane.thread_fold import fold
from agentplane.thread_fold.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadHistorySummary,
    ThreadPayloadChunk,
    ThreadPayloadManifest,
)
from agentplane.thread_fold.rows import ordered_entity_rows
from util.testing.undeclared_outputs import undeclared_outputs_dir

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize(
    ("history_size", "materialized_item_count"),
    [(200, 100), (4_000, 2_000), (40_000, 20_000)],
    ids=["one-hundred-items", "two-thousand-items", "twenty-thousand-items"],
)
async def test_command_lookup_and_touched_projection_preload_stay_indexed_with_large_history(
    store: ThreadStore,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
    engine: AsyncEngine,
    lease_for: LeaseFactory,
    history_size: int,
    materialized_item_count: int,
    request: pytest.FixtureRequest,
) -> None:
    """A real old-item update only preloads its touched rows after large frame and entity histories."""
    thread = await event_logs.open("sb-1", f"history-{history_size}")
    lease = await lease_for(thread)
    assert await ingestion.renew(lease, timedelta(minutes=10))
    command = command_pb2.Command(command_id="admission", submit_input=command_pb2.SubmitInput(text="saved"))
    admitted = event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command))
    await ingestion.record(
        thread,
        [admitted, event_entry(2, text_delta=event_pb2.TextDelta(item_id="old-item", text="before history"))],
        lease=lease,
    )
    # Keep the complete archive and equivalent item projection, but avoid measuring bulk prefix
    # folding here; this test measures indexed reads and a real update to its already-folded item.
    for start in range(3, history_size + 3, 100):
        stop = min(start + 100, history_size + 3)
        ingestion.peer.publish(
            thread, [_history_event(cursor, materialized_item_count) for cursor in range(start, stop)]
        )
    archive = ingestion.peer.histories[str(thread)].entries
    await _seed_indexed_history(thread, history_size, materialized_item_count, archive, engine)
    assert len(archive) == history_size + 2
    assert archive[-1] == _history_event(history_size + 2, materialized_item_count)

    scope = await content.current_scope(thread)
    assert scope is not None
    assert scope.through_cursor == history_size + 2
    # Warm the driver, typed codec, and Python caches before taking its allocation profile.
    assert await content.admitted_command(thread, command) == admitted
    captured: list[tuple[str, Any]] = []

    def capture_select(_: object, __: object, statement: str, parameters: Any, ___: object, ____: bool) -> None:
        if statement.lstrip().startswith("SELECT") and ("thread_entity" in statement or " FROM event" in statement):
            captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture_select)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        await ingestion.record(
            thread,
            [event_entry(history_size + 3, text_delta=event_pb2.TextDelta(item_id="old-item", text=" after history"))],
            lease=lease,
        )
        assert await content.admitted_command(thread, command) == admitted
        current, peak = tracemalloc.get_traced_memory()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
        event.remove(engine.sync_engine, "before_cursor_execute", capture_select)
    assert peak < 1_000_000, f"bounded record/read path allocated {peak} bytes for {history_size} historical rows"
    async with store._sessions() as session:
        item = await session.get(ThreadEntity, (thread, scope.projection_epoch, "item", "old-item"))
    assert item is not None
    assert item.revision_cursor == history_size + 3
    scope = await content.current_scope(thread)
    assert scope is not None
    assert scope.through_cursor == history_size + 3

    plans: list[str] = []
    async with engine.connect() as connection:
        # Use normal planner statistics after the actual write workload.  The artifact below is
        # deliberately the captured production statements, not a hand-written query with planner
        # switches, so its buffers compare point lookups across entity cardinalities.
        await connection.exec_driver_sql("ANALYZE thread_entity")
        for statement, parameters in captured:
            result = await connection.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {statement}", parameters)
            plans.extend(row[0] for row in result)
            plans.append("")
    assert captured
    profile = [
        f"history_size={history_size}",
        f"materialized_item_count={materialized_item_count}",
        f"tracemalloc_current={current}",
        f"tracemalloc_peak={peak}",
        "retained_allocations:",
        *(str(stat) for stat in after.compare_to(before, "lineno")[:20]),
        "captured_query_plans:",
        *plans,
    ]
    (undeclared_outputs_dir() / f"{request.node.name}-projection-profile.txt").write_text("\n".join(profile))


async def _seed_indexed_history(
    thread: UUID,
    history_size: int,
    materialized_item_count: int,
    archive: Sequence[event_log_pb2.EventEntry],
    engine: AsyncEngine,
) -> None:
    """Keep the archive full while seeding the unrelated, fully projected item pairs."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        checkpoint = await session.get(ThreadCheckpoint, thread)
        assert checkpoint is not None
        assert checkpoint.through_cursor == 2
        epoch = checkpoint.projection_epoch
        source_id = checkpoint.source_id
        result = fold.advance(
            fold.ViewState(fold.Position(source_id, epoch, through_cursor=2)),
            fold.EventBatch(source_id, 2, tuple(archive[2:]), ()),
            fold.PriorEntities(
                items={f"history-{index}": None for index in range(materialized_item_count)}, commands={}, bodies={}
            ),
        )
        assert result.state.position.through_cursor == history_size + 2
        next_index = await session.scalar(
            select(func.max(ThreadEntity.entity_index)).where(
                ThreadEntity.thread_id == thread, ThreadEntity.projection_epoch == epoch
            )
        )
        next_index = 0 if next_index is None else next_index + 1
        assert all(isinstance(write, fold.AppendPayload) and write.base is None for write in result.payload_writes)
        extents = {write.reference: int(bool(write.text)) for write in result.payload_writes}
        projected = ordered_entity_rows(thread, result, extents)
        items = [row for row in projected if row["entity_kind"] == "item"]
        view_state_row = next(row for row in projected if row["entity_kind"] == "view_state")
        assert len(items) == materialized_item_count
        assert len(projected) == materialized_item_count + 1
        for index, row in enumerate(items):
            row["entity_index"] = next_index + index
        await session.execute(insert(ThreadEntity), items)
        view_state = await session.get(ThreadEntity, (thread, epoch, "view_state", "current"))
        assert view_state is not None
        view_state.cursor = result.state.position.through_cursor
        view_state.revision_cursor = result.state.position.through_cursor
        assert isinstance(view_state_row["state"], dict)
        view_state.state = view_state_row["state"]
        checkpoint.through_cursor = result.state.position.through_cursor

        manifests = []
        chunks = []
        for write in result.payload_writes:
            assert isinstance(write, fold.AppendPayload)
            assert write.base is None
            reference = write.reference
            chunk_count = int(bool(write.text))
            manifests.append(
                {
                    "thread_id": thread,
                    "projection_epoch": reference.projection_epoch,
                    "owner_cursor": reference.owner_cursor,
                    "owner_id": reference.owner_id,
                    "field": reference.field,
                    "generation": reference.generation,
                    "revision_cursor": reference.revision_cursor,
                    "chunk_count": chunk_count,
                    "content_bytes": len(write.text.encode()),
                }
            )
            if chunk_count:
                chunks.append(
                    {
                        "thread_id": thread,
                        "projection_epoch": reference.projection_epoch,
                        "owner_cursor": reference.owner_cursor,
                        "owner_id": reference.owner_id,
                        "field": reference.field,
                        "generation": reference.generation,
                        "chunk_index": 0,
                        "text": write.text,
                    }
                )
        await session.execute(insert(ThreadPayloadManifest), manifests)
        if chunks:
            await session.execute(insert(ThreadPayloadChunk), chunks)
        evidence = [
            {
                "thread_id": thread,
                "projection_epoch": value.projection_epoch,
                "entity_cursor": value.entity_cursor,
                "observation_cursor": value.observation_cursor,
            }
            for value in result.evidence_upserts
        ]
        await session.execute(insert(ThreadEvidence), evidence)
        summary = await session.get(ThreadHistorySummary, thread)
        assert summary is not None
        summary.first_event_at = archive[0].event.at.ToDatetime(tzinfo=UTC)
        summary.last_event_at = archive[-1].event.at.ToDatetime(tzinfo=UTC)
        entity_count = await session.scalar(
            select(func.count())
            .select_from(ThreadEntity)
            .where(ThreadEntity.thread_id == thread, ThreadEntity.projection_epoch == epoch)
        )
        assert entity_count == materialized_item_count + 3
        manifest_count = await session.scalar(
            select(func.count())
            .select_from(ThreadPayloadManifest)
            .where(
                ThreadPayloadManifest.thread_id == thread,
                ThreadPayloadManifest.projection_epoch == epoch,
                ThreadPayloadManifest.owner_id.like("history-%"),
            )
        )
        assert manifest_count == materialized_item_count
        payload_chunk_count = await session.scalar(
            select(func.count())
            .select_from(ThreadPayloadChunk)
            .where(
                ThreadPayloadChunk.thread_id == thread,
                ThreadPayloadChunk.projection_epoch == epoch,
                ThreadPayloadChunk.owner_id.like("history-%"),
            )
        )
        assert payload_chunk_count == materialized_item_count
        evidence_count = await session.scalar(
            select(func.count())
            .select_from(ThreadEvidence)
            .where(
                ThreadEvidence.thread_id == thread,
                ThreadEvidence.projection_epoch == epoch,
                ThreadEvidence.entity_cursor >= 3,
            )
        )
        assert evidence_count == materialized_item_count * 2


def _history_event(cursor: int, materialized_item_count: int) -> event_log_pb2.EventEntry:
    index = cursor - 3
    if index < materialized_item_count * 2:
        item_id = f"history-{index // 2}"
        if index % 2 == 0:
            return event_entry(
                cursor, item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        return event_entry(cursor, text_delta=event_pb2.TextDelta(item_id=item_id, text="materialized"))
    return event_entry(
        cursor, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"type":"trace"}')
    )


if __name__ == "__main__":
    pytest_bazel.main()
