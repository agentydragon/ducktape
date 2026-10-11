"""The store's access paths under a large history: what a read touches must not grow with it."""

from __future__ import annotations

import gc
import tracemalloc
from itertools import batched
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import event, func, select

from agentplane.app.testing.history import SeededEventLogStore as EventLogStore
from agentplane.app.testing.thread_test_support import event_entry
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.history_service.fold.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadPayloadChunk,
    ThreadPayloadManifest,
)
from agentplane.history_service.testing.history import History
from agentplane.proto import command_pb2, event_log_pb2, event_pb2
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
    history: History,
    history_size: int,
    materialized_item_count: int,
    request: pytest.FixtureRequest,
) -> None:
    """A real old-item update only preloads its touched rows after large frame and entity histories."""
    thread = await event_logs.seed("sb-1", f"history-{history_size}")
    command = command_pb2.Command(command_id="admission", submit_input=command_pb2.SubmitInput(text="saved"))
    admitted = event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command))
    initial = [admitted, event_entry(2, text_delta=event_pb2.TextDelta(item_id="old-item", text="before history"))]
    await history.record(thread, initial)
    # The whole filler prefix is archived, but folded by the pure fold and bulk-seeded: this test
    # measures indexed reads and a real update to its already-folded item, not bulk prefix folding.
    filler = [_history_event(cursor, materialized_item_count) for cursor in range(3, history_size + 3)]
    async with history.paused():
        for batch in batched(filler, 1_000, strict=False):
            await history.store.append(thread, batch)
        await history.seed_folded(
            thread, [*initial, *filler], prior_items={f"history-{index}" for index in range(materialized_item_count)}
        )
    await _assert_seeded(thread, materialized_item_count, history)

    scope = await content.current_scope(thread)
    assert scope is not None
    # Warm the driver, typed codec, and Python caches before taking its allocation profile.
    assert await content.admitted_command(thread, command) == admitted
    captured: list[tuple[str, Any]] = []

    def capture_select(_: object, __: object, statement: str, parameters: Any, ___: object, ____: bool) -> None:
        if statement.lstrip().startswith("SELECT") and ("thread_entity" in statement or " FROM event" in statement):
            captured.append((statement, parameters))

    event.listen(history.engine.sync_engine, "before_cursor_execute", capture_select)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        await history.record(
            thread,
            [event_entry(history_size + 3, text_delta=event_pb2.TextDelta(item_id="old-item", text=" after history"))],
        )
        assert await content.admitted_command(thread, command) == admitted
        current, peak = tracemalloc.get_traced_memory()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
        event.remove(history.engine.sync_engine, "before_cursor_execute", capture_select)
    assert peak < 1_000_000, f"bounded record/read path allocated {peak} bytes for {history_size} historical rows"
    async with history.sessions() as session:
        item = await session.get(ThreadEntity, (thread, scope.projection_epoch, "item", "old-item"))
    assert item is not None
    assert item.revision_cursor == history_size + 3

    plans: list[str] = []
    async with history.engine.connect() as connection:
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


async def _assert_seeded(thread: UUID, materialized_item_count: int, history: History) -> None:
    """The bulk seed materialized every filler item, its payload and its evidence."""
    async with history.sessions() as session:
        checkpoint = await session.get(ThreadCheckpoint, thread)
        assert checkpoint is not None
        epoch = checkpoint.projection_epoch
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
