"""The event log's contract: one log per runner session, its entries read back in cursor order
without a runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.testing.thread_test_support import SPEC, Replica, event_entry
from agentplane.app.threads.events.event_log import EventLogStore, EventReplicationError
from agentplane.app.threads.events.ingestion_lease import IngestionLease
from agentplane.app.threads.ingestion import Ingestion
from agentplane.app.threads.models import EventLog, ThreadHistorySummary
from agentplane.protocol import event_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.client import SandboxServiceClient

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


async def test_remote_history_reads_its_own_committed_prefix(
    engine: AsyncEngine, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread_id = await event_logs.open("sb-1", "s-1", SPEC)
    entry = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))
    await ingestion.record(thread_id, [entry], lease=lease)

    class Reader:
        last_cursor = 0

        async def read_session_observations(
            self, session_id: str, *, limit: int
        ) -> protocol_pb2.ReadSessionObservationsResponse:
            assert session_id == str(thread_id)
            assert limit == 1
            return protocol_pb2.ReadSessionObservationsResponse(last_cursor=self.last_cursor)

        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> protocol_pb2.ReadSessionEventsResponse:
            assert session_id == str(thread_id)
            assert limit == 1
            return protocol_pb2.ReadSessionEventsResponse(
                last_cursor=self.last_cursor, entries=[entry] if after_cursor == 0 and self.last_cursor else []
            )

    reader = Reader()
    remote = EventLogStore(engine, history_reader=cast(SandboxServiceClient, reader))
    assert await remote.events(thread_id, limit=1) == []
    assert await remote.read_watermark(thread_id) == 0
    with pytest.raises(ConnectionError, match="not committed"):
        await remote.observation_entry(thread_id, 1)
    reader.last_cursor = 1
    assert await remote.events(thread_id, limit=1) == [entry]
    observed = await remote.observation_entry(thread_id, 1)
    assert observed is not None
    assert observed.entry["cursor"] == "1"
    reader.last_cursor = 2
    assert await remote.read_watermark(thread_id) == 2
    assert await remote.last_cursor(thread_id) == 1


async def test_service_read_does_not_chase_a_growing_watermark(engine: AsyncEngine) -> None:
    thread_id = UUID("00000000-0000-0000-0000-000000000001")
    calls = []

    class Reader:
        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> protocol_pb2.ReadSessionEventsResponse:
            calls.append(after_cursor)
            # A second page may advertise a newer head, but the call's ceiling is 2.
            end = 2 if after_cursor == 0 else 3
            return protocol_pb2.ReadSessionEventsResponse(
                last_cursor=end,
                entries=[event_entry(after_cursor + 1, harness_stderr=event_pb2.HarnessStderr(text="x"))],
            )

    remote = EventLogStore(engine, history_reader=cast(SandboxServiceClient, Reader()))
    assert [e.cursor for e in await remote.events(thread_id, limit=10)] == [1, 2]
    assert calls == [0, 1]


@pytest.mark.parametrize("case", ["gap", "beyond_watermark", "resume_ahead"])
async def test_service_read_rejects_invalid_pages(engine: AsyncEngine, case: str) -> None:
    thread_id = UUID("00000000-0000-0000-0000-000000000001")

    class Reader:
        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> protocol_pb2.ReadSessionEventsResponse:
            entries = [] if case != "beyond_watermark" else [event_entry(1)]
            return protocol_pb2.ReadSessionEventsResponse(last_cursor=1 if case == "gap" else 0, entries=entries)

    remote = EventLogStore(engine, history_reader=cast(SandboxServiceClient, Reader()))
    with pytest.raises(ConnectionError):
        await remote.events(thread_id, after_cursor=1 if case == "resume_ahead" else 0, limit=1)


async def test_concurrent_replicas_create_one_thread(event_logs: EventLogStore, replica: Replica) -> None:
    first, second = await asyncio.gather(
        event_logs.open("sb-1", "s-1", SPEC), replica.event_logs.open("sb-1", "s-1", SPEC)
    )
    assert first == second
    assert len(await replica.store.list_threads()) == 1


@pytest.mark.parametrize(
    ("before", "after", "cursors", "older", "newer"),
    [
        (None, None, [4, 5], "4", None),
        (4, None, [2, 3], "2", "3"),
        (None, 0, [1, 2], None, "2"),
        (0, None, [], None, None),
        (None, 5, [], None, None),
    ],
)
async def test_service_observation_pages_do_not_require_app_raw_rows(
    engine: AsyncEngine, before: int | None, after: int | None, cursors: list[int], older: str | None, newer: str | None
) -> None:
    thread = UUID("00000000-0000-0000-0000-000000000001")
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_observations.return_value = protocol_pb2.ReadSessionObservationsResponse(
        last_cursor=5,
        observations=[protocol_pb2.SessionObservation(cursor=cursor, kind="native") for cursor in cursors],
    )
    remote = EventLogStore(engine, history_reader=cast(SandboxServiceClient, reader))
    page = await remote.observations(thread, before_cursor=before, after_cursor=after, limit=2)
    assert [row.cursor for row in page.observations] == [str(cursor) for cursor in cursors]
    assert page.next_before_cursor == older
    assert page.next_after_cursor == newer
    reader.read_session_events.assert_not_awaited()
    reader.read_session_observations.assert_awaited_once_with(
        str(thread), before_cursor=before, after_cursor=after, limit=2
    )


async def test_service_observation_page_rejects_missing_entries(engine: AsyncEngine) -> None:
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_observations.return_value = protocol_pb2.ReadSessionObservationsResponse(last_cursor=5)
    remote = EventLogStore(engine, history_reader=cast(SandboxServiceClient, reader))
    with pytest.raises(ConnectionError, match="invalid service observation"):
        await remote.observations(UUID("00000000-0000-0000-0000-000000000001"), limit=2)


async def test_new_service_thread_is_atomically_fenced_and_idempotent(engine: AsyncEngine) -> None:
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_observations.return_value = protocol_pb2.ReadSessionObservationsResponse(last_cursor=0)
    left = EventLogStore(engine, history_creator=cast(SandboxServiceClient, reader))
    right = EventLogStore(engine, history_creator=cast(SandboxServiceClient, reader))
    public_id = uuid4()
    left_id, right_id = await asyncio.gather(
        left.open("sb-1", str(public_id), SPEC), right.open("sb-1", str(public_id), SPEC)
    )
    assert left_id == public_id
    assert right_id == public_id
    assert await left.is_raw_ingestion_fenced(public_id)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(ThreadHistorySummary)) == 1
        row = await session.get(ThreadHistorySummary, public_id)
        assert row is not None
        assert row.attached is None
    # A legacy replica observes the same ID, but cannot unfence it by opening it.
    assert await EventLogStore(engine).open("sb-1", str(public_id), SPEC) == public_id
    assert await right.is_raw_ingestion_fenced(public_id)


async def test_failed_service_registration_does_not_create_app_thread(engine: AsyncEngine) -> None:
    reader = AsyncMock(spec=SandboxServiceClient)
    reader.read_session_observations.side_effect = ConnectionError("service unavailable")
    store = EventLogStore(engine, history_creator=cast(SandboxServiceClient, reader))
    with pytest.raises(ConnectionError):
        await store.open("sb-1", str(uuid4()), SPEC)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(EventLog)) == 0
        assert await session.scalar(select(func.count()).select_from(ThreadHistorySummary)) == 0
    with pytest.raises(EventReplicationError, match="canonical"):
        await store.open("sb-1", "legacy-id", SPEC)


async def test_projection_mode_never_silently_hands_off_existing_thread(engine: AsyncEngine) -> None:
    legacy = EventLogStore(engine)
    public_id = uuid4()
    await legacy.open("sb-1", str(public_id), SPEC)
    reader = AsyncMock(spec=SandboxServiceClient)
    store = EventLogStore(engine, history_creator=cast(SandboxServiceClient, reader))
    assert await store.open("sb-1", str(public_id), SPEC) == public_id
    reader.read_session_observations.assert_not_awaited()
    assert not await store.is_raw_ingestion_fenced(public_id)


async def test_legacy_creator_winning_registration_race_remains_unfenced(engine: AsyncEngine) -> None:
    public_id = uuid4()
    legacy = EventLogStore(engine)
    reader = AsyncMock(spec=SandboxServiceClient)

    async def register(session_id: str, *, limit: int) -> protocol_pb2.ReadSessionObservationsResponse:
        assert session_id == str(public_id)
        assert limit == 1
        await legacy.open("sb-1", session_id, SPEC)
        return protocol_pb2.ReadSessionObservationsResponse(last_cursor=0)

    reader.read_session_observations.side_effect = register
    current = EventLogStore(engine, history_creator=cast(SandboxServiceClient, reader))
    assert await current.open("sb-1", str(public_id), SPEC) == public_id
    assert not await current.is_raw_ingestion_fenced(public_id)
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(ThreadHistorySummary)) == 0


if __name__ == "__main__":
    pytest_bazel.main()
