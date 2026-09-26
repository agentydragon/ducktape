"""The thread level: threads listed with their progress, and the name and archive state an operator
sets on one."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import text

from agentplane.app.agent_runtime.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.conftest import SPEC, event_entry
from agentplane.app.presets import Harness
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_threads_list_with_their_progress(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    empty = await event_logs.open(
        "sb-2", "s-9", protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CODEX, cwd="/w", model="m")
    )
    first = event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))
    second = event_entry(2, harness_lost=event_pb2.HarnessLost())
    second.event.at.FromDatetime(datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC))
    await ingestion.record(thread, [first, second], lease=lease)

    views = {view.id: view for view in await store.list_threads()}

    assert views[thread].model_dump(include={"sandbox", "session_id", "harness", "model", "cwd", "last_cursor"}) == {
        "sandbox": "sb-1",
        "session_id": "s-1",
        "harness": "HARNESS_CLAUDE",
        "model": "test-model",
        "cwd": "/state/work",
        "last_cursor": 2,
    }
    assert views[thread].harness is Harness.CLAUDE
    assert views[thread].last_event_at == datetime(2026, 9, 2, 12, 0, 1, tzinfo=UTC)
    assert (views[empty].harness, views[empty].last_cursor, views[empty].last_event_at) == ("HARNESS_CODEX", 0, None)
    assert views[empty].harness is Harness.CODEX
    # No feed has ever attached to either thread (only their event log was replayed), so the
    # exposed harness state stays unspecified rather than inferring it from history.
    assert views[thread].harness_state == views[empty].harness_state == "HARNESS_STATE_UNSPECIFIED"
    assert await store.get_thread(empty) == views[empty]
    assert await store.get_thread(thread) == views[thread]
    assert [view.id for view in await store.list_threads(sandbox="sb-1")] == [thread]
    assert [view.id for view in await store.list_threads(sandbox="sb-2", session_id="s-9")] == [empty]
    assert await store.list_threads(sandbox="sb-1", session_id="s-9") == []
    async with store._sessions() as session:
        await session.execute(text("SET LOCAL enable_seqscan = false"))
        plan = (
            await session.scalars(
                text("EXPLAIN (COSTS OFF) SELECT at FROM event WHERE thread_id = :thread ORDER BY at DESC LIMIT 1"),
                {"thread": thread},
            )
        ).all()
    assert any("ix_event_thread_at" in line for line in plan)


async def test_threads_list_reflects_the_attached_feed_s_harness_state(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    """`list_threads`/`get_thread` expose `FeedState.attached.harness_state` per thread — the live
    running/idle signal the sidebar's per-thread status dot reads (`agentplane/app/README.md`
    § Sidebar inventory updates), not a value derived from the historical event log."""
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    running_attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_RUNNING
    )
    await ingestion.set_attached(thread, running_attached, lease=lease)

    (running,) = await store.list_threads()
    assert running.harness_state == "HARNESS_STATE_RUNNING"
    got_thread = await store.get_thread(thread)
    assert got_thread is not None
    assert got_thread.harness_state == "HARNESS_STATE_RUNNING"

    stopped_attached = protocol_pb2.Attached(
        session_id="s-1", spec=SPEC, harness_state=protocol_pb2.HARNESS_STATE_STOPPED
    )
    await ingestion.set_attached(thread, stopped_attached, lease=lease)
    (stopped,) = await store.list_threads()
    assert stopped.harness_state == "HARNESS_STATE_STOPPED"


async def test_a_thread_is_unnamed_until_renamed_and_keeps_its_progress(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))], lease=lease)
    (unnamed,) = await store.list_threads()
    assert unnamed.name is None

    renamed = await store.rename(thread, "list the files")

    assert (renamed.name, renamed.last_cursor) == ("list the files", 1)
    assert await store.get_thread(thread) == renamed
    assert (await store.rename(thread, None)).name is None
    with pytest.raises(ThreadNotFoundError):
        await store.rename(UUID(int=0), "nobody")


async def test_a_thread_archives_and_unarchives_without_touching_its_progress(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1))], lease=lease)
    (unarchived,) = await store.list_threads()
    assert unarchived.archived is False

    archived = await store.archive(thread)

    assert (archived.archived, archived.last_cursor) == (True, 1)
    assert await store.get_thread(thread) == archived
    assert await store.list_threads() == []
    assert [view.id for view in await store.list_threads(include_archived=True)] == [thread]

    unarchived = await store.unarchive(thread)
    assert unarchived.archived is False
    assert [view.id for view in await store.list_threads()] == [thread]
    with pytest.raises(ThreadNotFoundError):
        await store.archive(UUID(int=0))


if __name__ == "__main__":
    pytest_bazel.main()
