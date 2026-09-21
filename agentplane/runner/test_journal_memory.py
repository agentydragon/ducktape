"""Profile Python allocations while reopening and paging differently sized histories."""

import gc
import json
import sqlite3
import tracemalloc
from pathlib import Path

import pytest_bazel

from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner.journal import Journal
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


async def _seed(path: Path, count: int) -> None:
    async with Journal.open(path, "profile-source"):
        pass
    # Bulk fixture construction keeps this a recovery/read profile, not an fsync benchmark.
    with sqlite3.connect(path) as connection:
        for after in range(0, count, 128):
            entries = [
                event_log_pb2.EventEntry(
                    cursor=cursor,
                    origin=event_log_pb2.EventOrigin(source_id="profile-source", sequence=cursor),
                    event=event_pb2.Event(native=event_pb2.Native(line="x" * 1024)),
                )
                for cursor in range(after + 1, min(after + 128, count) + 1)
            ]
            connection.executemany(
                "INSERT INTO event_entry (cursor, payload) VALUES (?, ?)",
                [(entry.cursor, entry.SerializeToString()) for entry in entries],
            )
        connection.execute("UPDATE checkpoint SET through_cursor = ? WHERE id = 1", (count,))


async def test_recovery_and_page_allocations_do_not_scale_with_history(tmp_path: Path) -> None:
    counts = (512, 32768)
    peaks: list[int] = []
    for count in counts:
        path = tmp_path / f"history-{count}.sqlite"
        await _seed(path, count)
        gc.collect()
        tracemalloc.start()
        try:
            async with Journal.open(path, "profile-source") as journal:
                page = await journal.since(count - 128, limit=128)
                assert len(page) == 128
                assert page[0].cursor == count - 127
                assert page[-1].cursor == count
                _, peak = tracemalloc.get_traced_memory()
                peaks.append(peak)
            del page
        finally:
            tracemalloc.stop()
    (undeclared_outputs_dir() / "journal-memory.json").write_text(
        json.dumps({"history_events": counts, "python_peak_bytes": peaks, "page_events": 128}, indent=2)
    )
    # Allow interpreter/driver allocation noise, but not even one extra MiB of historical data.
    assert peaks[1] <= peaks[0] + 1024 * 1024


if __name__ == "__main__":
    pytest_bazel.main()
