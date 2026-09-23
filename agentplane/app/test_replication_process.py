"""SIGKILL the app at PostgreSQL ingestion boundaries, then reconnect through a new replica.

The protocol source is controlled: these tests prove app replication, not native harness semantics
or power-loss durability. The browser is an actual HTTP/SSE client, not an in-process iterator.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import ParseDict
from httpx_sse import ServerSentEvent, aconnect_sse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.testing.replication_process import CommitBoundary, app_process
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.thread.content import ContentStore
from agentplane.app.thread.models import SandboxIngestion
from agentplane.app.thread.store import ThreadStore
from agentplane.app.thread.updates import ThreadUpdates
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg


async def next_entry(stream: AsyncIterator[ServerSentEvent]) -> event_log_pb2.EventEntry:
    message = await anext(stream)
    assert message.event == "event"
    entry = ParseDict(message.json(), event_log_pb2.EventEntry())
    assert message.id == str(entry.cursor)
    return entry


async def wait_snapshot(
    event_logs: EventLogStore, updates: ThreadUpdates, thread: UUID, cursor: int
) -> protocol_pb2.Attached:
    changed = asyncio.Event()
    with updates.changes.subscribe(changed):
        while True:
            changed.clear()
            snapshot = await event_logs.feed_state(thread)
            if snapshot is not None and snapshot.attached.last_cursor == cursor:
                return snapshot.attached
            await changed.wait()


@pytest.mark.parametrize("boundary", list(CommitBoundary))
async def test_killed_ingester_recovers_exact_prefix_and_browser_handoff(
    db_url: str,
    store: ThreadStore,
    event_logs: EventLogStore,
    content: ContentStore,
    thread_updates: ThreadUpdates,
    boundary: CommitBoundary,
) -> None:
    source = ReplicationSource()
    source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
    thread_id = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
    events = f"/threads/{thread_id}/events/stream"
    async with asyncio.timeout(45), source.serve() as runner_port:
        async with (
            app_process(db_url, runner_port, boundary=boundary, cursor=4) as first,
            httpx.AsyncClient(base_url=first.url, timeout=None) as browser,
            aconnect_sse(browser, "GET", events) as connection,
        ):
            opened = await source.opened.get()
            assert opened.after_cursor == 0
            opened.replay.set()
            stream = connection.aiter_sse()
            assert (await anext(stream)).event == "attached"
            assert await next_entry(stream) == source.entries[0]
            source.attached.active_turn_id = "test-turn"
            source.append(
                event_pb2.Event(
                    turn_started=event_pb2.TurnStarted(turn_id="test-turn", model=source.attached.spec.model)
                )
            )
            assert await next_entry(stream) == source.entries[1]
            source.append(
                event_pb2.Event(
                    command_admitted=event_pb2.CommandAdmitted(
                        command=command_pb2.Command(
                            command_id="test-model-command",
                            change_model=command_pb2.ChangeModel(model="test-model-after"),
                        )
                    )
                )
            )
            assert await next_entry(stream) == source.entries[2]
            # The browser stops consuming here; it never observes the command's effect before death.
            (thread,) = await store.list_threads(sandbox=SANDBOX)
            source.attached.spec.model = "test-model-after"
            source.append(
                event_pb2.Event(
                    model_changed=event_pb2.ModelChanged(
                        command_id="test-model-command", previous_model="test-model-before", model="test-model-after"
                    )
                )
            )
            assert (await first.checkpoint()).boundary is boundary
            committed = 3 if boundary is CommitBoundary.BEFORE else 4
            # Independent PostgreSQL reads, while the app is paused at a real commit boundary.
            assert await event_logs.events(thread.id, limit=100) == source.entries[:committed]
            projection = await content.current_scope(thread.id)
            assert projection is not None
            assert projection.through_cursor == committed
            before_death = await event_logs.feed_state(thread.id)
            assert before_death is not None
            assert before_death.attached.last_cursor == committed
            assert before_death.attached.active_turn_id == "test-turn"
            assert before_death.attached.spec.model == (
                "test-model-before" if boundary is CommitBoundary.BEFORE else "test-model-after"
            )
            await first.kill()

        # SIGKILL does not release the lease gracefully. Advance its database expiry explicitly
        # rather than sleeping for the production lease interval; takeover still uses real fencing.
        engine = create_async_engine(db_url)
        try:
            async with engine.begin() as database:
                old_token = await database.scalar(
                    select(SandboxIngestion.token).where(SandboxIngestion.sandbox == SANDBOX)
                )
                assert old_token is not None
                await database.execute(
                    update(SandboxIngestion)
                    .where(SandboxIngestion.sandbox == SANDBOX)
                    .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
                )

            assert await event_logs.events(thread.id, limit=100) == source.entries[:committed]
            assert await event_logs.feed_state(thread.id) == before_death
            # The runner source continues independently while no app process is alive.
            source.append(
                event_pb2.Event(
                    native=event_pb2.Native(
                        direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"test":"retained raw frame"}'
                    )
                )
            )
            source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="test-item", text="retained text")))
            async with (
                app_process(db_url, runner_port) as successor,
                httpx.AsyncClient(base_url=successor.url, timeout=None) as reconnected,
            ):
                # Discovery first inspects the runner, then reattaches including the archived
                # boundary entry. Keep replay held so the snapshot/archive distinction is visible.
                assert (await source.opened.get()).after_cursor == 0
                replay = await source.opened.get()
                assert replay.after_cursor == committed - 1
                attached = await wait_snapshot(event_logs, thread_updates, thread.id, 6)
                assert attached == source.attached
                assert await event_logs.last_cursor(thread.id) == committed
                async with engine.connect() as database:
                    new_token = await database.scalar(
                        select(SandboxIngestion.token).where(SandboxIngestion.sandbox == SANDBOX)
                    )
                    assert new_token is not None
                    assert new_token != old_token

                # Last-Event-ID wins over the stale query cursor. The snapshot is explicitly a
                # runner observation at 6, not proof that the app has copied through cursor 6.
                async with aconnect_sse(
                    reconnected, "GET", events + "?after=0", headers={"Last-Event-ID": "3"}
                ) as resumed:
                    stream = resumed.aiter_sse()
                    snapshot = await anext(stream)
                    assert snapshot.event == "attached"
                    assert ParseDict(snapshot.json(), protocol_pb2.Attached()) == source.attached
                    replay.replay.set()
                    assert await next_entry(stream) == source.entries[3]
                    # Append during browser catch-up: replay and live share exactly one prefix.
                    source.append(
                        event_pb2.Event(
                            item_completed=event_pb2.ItemCompleted(item_id="test-item", text="retained text")
                        )
                    )
                    for entry in source.entries[4:]:
                        assert await next_entry(stream) == entry
                    # Now the browser has exhausted replay. A new NOTIFY wakes its live read.
                    source.attached.active_turn_id = ""
                    final = source.append(
                        event_pb2.Event(
                            turn_completed=event_pb2.TurnCompleted(
                                turn_id="test-turn", status=event_pb2.TURN_STATUS_COMPLETED
                            )
                        )
                    )
                    assert await next_entry(stream) == final

                assert await event_logs.events(thread.id, limit=100) == source.entries
                final_snapshot = await event_logs.feed_state(thread.id)
                assert final_snapshot is not None
                assert final_snapshot.end is None
                assert final_snapshot.attached == source.attached
                view = await store.get_thread(thread.id)
                assert view is not None
                assert (view.last_cursor, view.model) == (8, "test-model-after")
                projection = await content.current_scope(thread.id)
                assert projection is not None
                assert projection.through_cursor == view.last_cursor
        finally:
            await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
