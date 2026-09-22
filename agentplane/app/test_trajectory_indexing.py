"""The store's access paths under a large history: what a read touches must not grow with it."""

from __future__ import annotations

import gc
import tracemalloc
from datetime import timedelta
from typing import Any

import pytest
import pytest_bazel
from sqlalchemy import event, select

from agentplane.app.conftest import SPEC, event_entry
from agentplane.app.trajectory import IngestionLease, ThreadEntity, TrajectoryStore
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize(
    ("history_size", "materialized_item_count"),
    [(200, 100), (4_000, 2_000), (40_000, 20_000)],
    ids=["one-hundred-items", "two-thousand-items", "twenty-thousand-items"],
)
async def test_command_lookup_and_touched_projection_preload_stay_indexed_with_large_history(
    store: TrajectoryStore,
    lease: IngestionLease,
    history_size: int,
    materialized_item_count: int,
    request: pytest.FixtureRequest,
) -> None:
    """A real old-item update only preloads its touched rows after large frame and entity histories."""
    # This test deliberately creates 20,000 materialized entities in bounded record batches.
    # Keep the writer fence valid for that workload; this does not change the test timeout.
    assert await store.renew_ingestion(lease, timedelta(minutes=10))
    thread = await store.thread("sb-1", f"history-{history_size}", SPEC)
    command = command_pb2.Command(command_id="admission", submit_input=command_pb2.SubmitInput(text="saved"))
    admitted = event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command))
    await store.record(
        thread,
        [admitted, event_entry(2, text_delta=event_pb2.TextDelta(item_id="old-item", text="before history"))],
        lease=lease,
    )
    for start in range(3, history_size + 3, 100):
        stop = min(start + 100, history_size + 3)
        await store.record(
            thread, [_history_event(cursor, materialized_item_count) for cursor in range(start, stop)], lease=lease
        )

    scope = await store.current_scope(thread)
    assert scope is not None
    # Warm the driver, typed codec, and Python caches before taking its allocation profile.
    assert await store.admitted_command(thread, command) == admitted
    assert await store.command_outcomes(thread, scope.projection_epoch, ["admission", "absent"]) == {
        "admission": "pending",
        "absent": None,
    }
    captured: list[tuple[str, Any]] = []

    def capture_select(_: object, __: object, statement: str, parameters: Any, ___: object, ____: bool) -> None:
        if statement.lstrip().startswith("SELECT") and ("thread_entity" in statement or " FROM event" in statement):
            captured.append((statement, parameters))

    event.listen(store._engine.sync_engine, "before_cursor_execute", capture_select)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        await store.record(
            thread,
            [event_entry(history_size + 3, text_delta=event_pb2.TextDelta(item_id="old-item", text=" after history"))],
            lease=lease,
        )
        assert await store.admitted_command(thread, command) == admitted
        assert await store.command_outcomes(thread, scope.projection_epoch, ["admission", "absent"]) == {
            "admission": "pending",
            "absent": None,
        }
        current, peak = tracemalloc.get_traced_memory()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
        event.remove(store._engine.sync_engine, "before_cursor_execute", capture_select)
    assert peak < 1_000_000, f"bounded record/read path allocated {peak} bytes for {history_size} historical rows"
    async with store._sessions() as session:
        item = await session.get(ThreadEntity, (thread, scope.projection_epoch, "item", "old-item"))
    assert item is not None
    assert item.revision_cursor == history_size + 3

    plans: list[str] = []
    async with store._engine.connect() as connection:
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


async def test_segment_tail_uses_partial_cursor_index_after_many_settled_commands(
    store: TrajectoryStore, lease: IngestionLease, request: pytest.FixtureRequest
) -> None:
    """Tail-window bounds do not walk settled commands that sort after the last segment."""
    assert await store.renew_ingestion(lease, timedelta(minutes=10))
    thread = await store.thread("sb-1", "command-dense-tail", SPEC)
    await store.record(
        thread,
        [
            event_entry(
                1, item_started=event_pb2.ItemStarted(item_id="only-segment", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        ],
        lease=lease,
    )
    cursor = 2
    for batch_start in range(0, 10_000, 50):
        batch: list[event_log_pb2.EventEntry] = []
        for command_index in range(batch_start, batch_start + 50):
            command_id = f"settled-{command_index}"
            batch.append(
                event_entry(
                    cursor,
                    command_admitted=event_pb2.CommandAdmitted(
                        command=command_pb2.Command(
                            command_id=command_id, submit_input=command_pb2.SubmitInput(text="saved")
                        )
                    ),
                )
            )
            cursor += 1
            batch.append(event_entry(cursor, command_noop=event_pb2.CommandNoop(command_id=command_id, reason="done")))
            cursor += 1
        await store.record(thread, batch, lease=lease)

    scope = await store.current_scope(thread)
    assert scope is not None
    captured: list[tuple[str, Any]] = []

    def capture_select(_: object, __: object, statement: str, parameters: Any, ___: object, ____: bool) -> None:
        if statement.lstrip().startswith("SELECT") and "thread_entity" in statement:
            captured.append((statement, parameters))

    event.listen(store._engine.sync_engine, "before_cursor_execute", capture_select)
    try:
        async with store._sessions() as session:
            cursors = list(
                await session.scalars(
                    select(ThreadEntity.cursor)
                    .where(
                        ThreadEntity.thread_id == thread,
                        ThreadEntity.projection_epoch == scope.projection_epoch,
                        ThreadEntity.entity_kind.in_(("item", "confirmed_input", "lifecycle")),
                        ThreadEntity.cursor < scope.through_cursor + 1,
                    )
                    .order_by(ThreadEntity.cursor.desc())
                    .limit(30)
                )
            )
    finally:
        event.remove(store._engine.sync_engine, "before_cursor_execute", capture_select)
    assert cursors == [1]
    assert len(captured) == 1
    async with store._engine.connect() as connection:
        await connection.exec_driver_sql("ANALYZE thread_entity")
        statement, parameters = captured[0]
        result = await connection.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {statement}", parameters)
        plan = "\n".join(row[0] for row in result)
    assert "ix_thread_entity_scope_segment_cursor" in plan
    assert "Rows Removed by Filter" not in plan
    (undeclared_outputs_dir() / f"{request.node.name}-segment-tail-profile.txt").write_text(
        f"settled_command_count=10000\n{plan}\n"
    )


if __name__ == "__main__":
    pytest_bazel.main()
