"""A commit on one replica wakes readers on another, including across a listener reconnect."""

from __future__ import annotations

import asyncio

import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.conftest import SPEC, Replica, event_entry
from agentplane.app.ingestion import Ingestion
from agentplane.app.thread.store import ThreadStore
from agentplane.app.thread.updates import ThreadUpdates
from agentplane.protocol import event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_commits_wake_another_replica_and_leave_durable_replay(
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    replica: Replica,
    thread_updates: ThreadUpdates,
    lease: IngestionLease,
) -> None:
    changed = asyncio.Event()
    with thread_updates.changes.subscribe(changed):
        thread = await event_logs.open("sb-1", "s-1", SPEC)
        await asyncio.wait_for(changed.wait(), timeout=5)
        assert (await replica.store.list_threads())[0].id == thread
        changed.clear()
        await store.rename(thread, "cross-replica rename")
        await asyncio.wait_for(changed.wait(), timeout=5)
        view = await replica.store.get_thread(thread)
        assert view is not None
        assert view.name == "cross-replica rename"
        changed.clear()
        await ingestion.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
        await asyncio.wait_for(changed.wait(), timeout=5)
        assert [entry.cursor for entry in await replica.event_logs.events(thread, limit=10)] == [1]


async def test_listener_reconnect_wakes_readers_for_writes_during_the_gap(
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    replica: Replica,
    thread_updates: ThreadUpdates,
    lease: IngestionLease,
    db_url: str,
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    engine = create_async_engine(db_url)
    changed = asyncio.Event()
    try:
        with thread_updates.changes.subscribe(changed):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND application_name = 'agentplane-thread-updates'"
                    )
                )
            # Wait on the termination callback itself, rather than an untagged Changes wakeup:
            # a previous database notification can arrive after the subscription starts.
            await asyncio.wait_for(thread_updates.wait_until_disconnected(), timeout=5)
            assert not thread_updates.connected
            changed.clear()
            await ingestion.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
            await asyncio.wait_for(changed.wait(), timeout=5)
            assert thread_updates.connected
            assert [entry.cursor for entry in await replica.event_logs.events(thread, limit=10)] == [1]
            # Establish another live write still wakes this replica after it reconnects.
            changed.clear()
            await store.rename(thread, "after reconnect")
            await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
