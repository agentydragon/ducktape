"""The event log's contract: one log per runner session, its entries read back in cursor order
without a runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest_bazel

from agentplane.app.conftest import SPEC, Replica, event_entry
from agentplane.app.ingestion import Ingestion
from agentplane.app.thread.event_log import EventLogStore
from agentplane.app.thread.ingestion_lease import IngestionLease
from agentplane.protocol import event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_a_session_is_one_thread_and_its_events_read_back_in_order(
    event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    assert await event_logs.open("sb-1", "s-1", SPEC) == thread
    other = await event_logs.open("sb-1", "s-2", SPEC)
    assert other != thread

    await ingestion.record(
        thread,
        [
            event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7)),
            event_entry(2, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"type":"x"}')),
            event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="t1")),
        ],
        lease=lease,
    )
    # A replay after a reconnect brings sequences already stored: they are not written twice.
    await ingestion.record(
        thread,
        [
            event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="t1")),
            event_entry(4, harness_lost=event_pb2.HarnessLost()),
        ],
        lease=lease,
    )

    entries = await event_logs.events(thread, limit=100)
    assert [entry.cursor for entry in entries] == [1, 2, 3, 4]
    assert entries[1].event.native.line == '{"type":"x"}'
    assert entries[1].event.at.ToDatetime(tzinfo=UTC) == datetime(2026, 9, 2, 12, 0, 2, tzinfo=UTC)
    assert [entry.cursor for entry in await event_logs.events(thread, after_cursor=2, limit=100)] == [3, 4]
    assert [entry.cursor for entry in await event_logs.events(thread, after_cursor=1, limit=2)] == [2, 3]
    assert await event_logs.last_cursor(thread) == 4
    assert await event_logs.last_cursor(other) == 0


async def test_concurrent_replicas_create_one_thread(event_logs: EventLogStore, replica: Replica) -> None:
    first, second = await asyncio.gather(
        event_logs.open("sb-1", "s-1", SPEC), replica.event_logs.open("sb-1", "s-1", SPEC)
    )
    assert first == second
    assert len(await replica.store.list_threads()) == 1


if __name__ == "__main__":
    pytest_bazel.main()
