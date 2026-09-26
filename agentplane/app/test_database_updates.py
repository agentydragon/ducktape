"""A commit on one replica wakes its channel's readers on another, including across a listener reconnect."""

from __future__ import annotations

import asyncio

import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.conftest import SPEC, Replica, event_entry
from agentplane.app.database_updates import Channel, DatabaseUpdates
from agentplane.protocol import event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_commits_wake_another_replica_and_leave_durable_replay(
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    replica: Replica,
    database_updates: DatabaseUpdates,
    lease: IngestionLease,
) -> None:
    changed, sessions_changed = asyncio.Event(), asyncio.Event()
    with (
        database_updates.changes[Channel.THREADS].subscribe(changed),
        database_updates.changes[Channel.OPERATOR_SESSIONS].subscribe(sessions_changed),
    ):
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
        # Each notification was dispatched to its own channel's readers only, in the callback that
        # woke the thread readers above.
        assert not sessions_changed.is_set()


async def test_listener_reconnect_wakes_every_channel_for_writes_during_the_gap(
    store: ThreadStore,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    replica: Replica,
    database_updates: DatabaseUpdates,
    lease: IngestionLease,
    db_url: str,
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    engine = create_async_engine(db_url)
    changed, sessions_changed = asyncio.Event(), asyncio.Event()
    try:
        with (
            database_updates.changes[Channel.THREADS].subscribe(changed),
            database_updates.changes[Channel.OPERATOR_SESSIONS].subscribe(sessions_changed),
        ):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND application_name = 'agentplane-database-updates'"
                    )
                )
            # Wait on the termination callback itself, rather than an untagged Changes wakeup:
            # a previous database notification can arrive after the subscription starts.
            await asyncio.wait_for(database_updates.wait_until_disconnected(), timeout=5)
            assert not database_updates.connected
            changed.clear()
            sessions_changed.clear()
            await ingestion.record(thread, [event_entry(1, harness_lost=event_pb2.HarnessLost())], lease=lease)
            await asyncio.wait_for(changed.wait(), timeout=5)
            assert database_updates.connected
            assert [entry.cursor for entry in await replica.event_logs.events(thread, limit=10)] == [1]
            # Nothing announced a session change: only the reconnect itself wakes that channel's readers.
            await asyncio.wait_for(sessions_changed.wait(), timeout=5)
            # Establish another live write still wakes this replica after it reconnects.
            changed.clear()
            await store.rename(thread, "after reconnect")
            await asyncio.wait_for(changed.wait(), timeout=5)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
