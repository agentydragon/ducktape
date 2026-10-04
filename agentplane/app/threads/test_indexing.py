"""The store's access paths under a large history: what a read touches must not grow with it."""

from __future__ import annotations

import gc
import tracemalloc
from datetime import timedelta
from typing import Any

import pytest
import pytest_bazel
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.conftest import SPEC, event_entry
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.events.ingestion_lease import IngestionLease
from agentplane.app.threads.ingestion import Ingestion
from agentplane.app.threads.models import ThreadEntity
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
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
    store: ThreadStore,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
    engine: AsyncEngine,
    lease: IngestionLease,
    history_size: int,
    materialized_item_count: int,
    request: pytest.FixtureRequest,
) -> None:
    """A real old-item update only preloads its touched rows after large frame and entity histories."""
    # This test deliberately creates 20,000 materialized entities in bounded record batches.
    # Keep the writer fence valid for that workload; this does not change the test timeout.
    assert await ingestion.renew(lease, timedelta(minutes=10))
    thread = await event_logs.open("sb-1", f"history-{history_size}", SPEC)
    command = command_pb2.Command(command_id="admission", submit_input=command_pb2.SubmitInput(text="saved"))
    admitted = event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command))
    await ingestion.record(
        thread,
        [admitted, event_entry(2, text_delta=event_pb2.TextDelta(item_id="old-item", text="before history"))],
        lease=lease,
    )
    for start in range(3, history_size + 3, 100):
        stop = min(start + 100, history_size + 3)
        await ingestion.record(
            thread, [_history_event(cursor, materialized_item_count) for cursor in range(start, stop)], lease=lease
        )

    scope = await content.current_scope(thread)
    assert scope is not None
    # Warm the driver, typed codec, and Python caches before taking its allocation profile.
    assert await content.admitted_command(thread, command) == admitted
    assert await content.command_outcomes(thread, scope.projection_epoch, ["admission", "absent"]) == {
        "admission": "pending",
        "absent": None,
    }
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
        assert await content.command_outcomes(thread, scope.projection_epoch, ["admission", "absent"]) == {
            "admission": "pending",
            "absent": None,
        }
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
