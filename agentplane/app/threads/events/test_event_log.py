"""Production service-backed archive reads and retained identity compatibility."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.testing.retained_history import seed_retained_session
from agentplane.app.testing.thread_test_support import event_entry
from agentplane.app.threads.events.event_log import EventLogStore, SessionAdmissionError
from agentplane.app.threads.models import EventLog
from agentplane.app.threads.summaries import Summaries
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_pb2, history_service_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_remote_history_reads_its_own_committed_prefix(engine: AsyncEngine) -> None:
    thread_id = await seed_retained_session(engine)
    entry = event_entry(1, harness_started=event_pb2.HarnessStarted(resumed=False, pid=7))

    class Reader:
        last_cursor = 0

        async def get_session_summary(self, session_id: str) -> history_service_pb2.SessionSummary:
            # The fold has applied the first entry, whatever the archive has committed since.
            return history_service_pb2.SessionSummary(session_id=session_id, folded_through=1)

        async def read_session_observations(
            self, session_id: str, *, limit: int
        ) -> history_service_pb2.ReadSessionObservationsResponse:
            assert session_id == str(thread_id)
            assert limit == 1
            return history_service_pb2.ReadSessionObservationsResponse(last_cursor=self.last_cursor)

        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> history_service_pb2.ReadSessionEventsResponse:
            assert session_id == str(thread_id)
            assert limit == 1
            return history_service_pb2.ReadSessionEventsResponse(
                last_cursor=self.last_cursor, entries=[entry] if after_cursor == 0 and self.last_cursor else []
            )

    reader = Reader()
    remote = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
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
        ) -> history_service_pb2.ReadSessionEventsResponse:
            calls.append(after_cursor)
            # A second page may advertise a newer head, but the call's ceiling is 2.
            end = 2 if after_cursor == 0 else 3
            return history_service_pb2.ReadSessionEventsResponse(
                last_cursor=end,
                entries=[event_entry(after_cursor + 1, harness_stderr=event_pb2.HarnessStderr(text="x"))],
            )

    reader = cast(HistoryServiceClient, Reader())
    remote = EventLogStore(engine, summaries=Summaries(engine, reader), history_reader=reader)
    assert [e.cursor for e in await remote.events(thread_id, limit=10)] == [1, 2]
    assert calls == [0, 1]


# Cursors 3-5 were settled; 6 completes the item.
SETTLED = history_service_pb2.SettledDeltas(
    item_id="test-item", ranges=[history_service_pb2.CursorRange(first=3, last=5)]
)


async def test_service_read_walks_over_settled_ranges(engine: AsyncEngine) -> None:
    thread_id = UUID("00000000-0000-0000-0000-000000000001")
    stored = {cursor: event_entry(cursor) for cursor in (1, 2, 6, 7)}

    class Reader:
        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> history_service_pb2.ReadSessionEventsResponse:
            entries = [entry for cursor, entry in stored.items() if cursor > after_cursor][:limit]
            return history_service_pb2.ReadSessionEventsResponse(last_cursor=7, entries=entries, settlements=[SETTLED])

        async def read_session_observations(
            self, session_id: str, *, limit: int
        ) -> history_service_pb2.ReadSessionObservationsResponse:
            return history_service_pb2.ReadSessionObservationsResponse(last_cursor=7)

    reader = cast(HistoryServiceClient, Reader())
    remote = EventLogStore(engine, summaries=Summaries(engine, reader), history_reader=reader)
    assert [entry.cursor for entry in await remote.events(thread_id, limit=10)] == [1, 2, 6, 7]
    assert await remote.observation_entry(thread_id, 4) is None
    observed = await remote.observation_entry(thread_id, 6)
    assert observed is not None
    assert observed.entry["cursor"] == "6"


@pytest.mark.parametrize("case", ["gap", "beyond_watermark", "resume_ahead"])
async def test_service_read_rejects_invalid_pages(engine: AsyncEngine, case: str) -> None:
    thread_id = UUID("00000000-0000-0000-0000-000000000001")

    class Reader:
        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> history_service_pb2.ReadSessionEventsResponse:
            entries = [] if case != "beyond_watermark" else [event_entry(1)]
            return history_service_pb2.ReadSessionEventsResponse(last_cursor=1 if case == "gap" else 0, entries=entries)

    reader = cast(HistoryServiceClient, Reader())
    remote = EventLogStore(engine, summaries=Summaries(engine, reader), history_reader=reader)
    with pytest.raises(ConnectionError):
        await remote.events(thread_id, after_cursor=1 if case == "resume_ahead" else 0, limit=1)


async def test_concurrent_replicas_create_one_service_thread(engine: AsyncEngine) -> None:
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(last_cursor=0)
    stores = [EventLogStore(engine, summaries=Summaries(engine, reader), history_reader=reader) for _ in range(2)]
    public_id = uuid4()
    first, second = await asyncio.gather(*(store.open(str(public_id)) for store in stores))
    assert first == second == public_id
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(EventLog)) == 1


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
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(
        last_cursor=5,
        observations=[history_service_pb2.SessionObservation(cursor=cursor, kind="native") for cursor in cursors],
    )
    remote = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    page = await remote.observations(thread, before_cursor=before, after_cursor=after, limit=2)
    assert [row.cursor for row in page.observations] == [str(cursor) for cursor in cursors]
    assert page.next_before_cursor == older
    assert page.next_after_cursor == newer
    reader.read_session_events.assert_not_awaited()
    reader.read_session_observations.assert_awaited_once_with(
        str(thread), before_cursor=before, after_cursor=after, limit=2
    )


@pytest.mark.parametrize(
    ("before", "after", "cursors"),
    [(None, None, [6, 7]), (7, None, [2, 6]), (5, None, [1, 2]), (None, 2, [6, 7]), (None, 6, [7])],
)
async def test_service_observation_pages_treat_settled_ranges_as_covered(
    engine: AsyncEngine, before: int | None, after: int | None, cursors: list[int]
) -> None:
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(
        last_cursor=7,
        observations=[history_service_pb2.SessionObservation(cursor=cursor, kind="native") for cursor in cursors],
        settled=SETTLED.ranges,
    )
    client = cast(HistoryServiceClient, reader)
    remote = EventLogStore(engine, summaries=Summaries(engine, client), history_reader=client)
    page = await remote.observations(
        UUID("00000000-0000-0000-0000-000000000001"), before_cursor=before, after_cursor=after, limit=2
    )
    assert [row.cursor for row in page.observations] == [str(cursor) for cursor in cursors]


async def test_service_observation_page_rejects_missing_entries(engine: AsyncEngine) -> None:
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(last_cursor=5)
    remote = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    with pytest.raises(ConnectionError, match="invalid service observation"):
        await remote.observations(UUID("00000000-0000-0000-0000-000000000001"), limit=2)


async def test_failed_service_registration_does_not_create_app_thread(engine: AsyncEngine) -> None:
    reader = AsyncMock(spec=HistoryServiceClient)
    reader.read_session_observations.side_effect = ConnectionError("service unavailable")
    store = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    with pytest.raises(ConnectionError):
        await store.open(str(uuid4()))
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(EventLog)) == 0
    with pytest.raises(SessionAdmissionError, match="canonical"):
        await store.open("legacy-id")


async def test_public_session_open_preserves_retained_identity(engine: AsyncEngine) -> None:
    public_id = await seed_retained_session(engine)
    reader = AsyncMock(spec=HistoryServiceClient)
    current = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    assert await current.open(str(public_id)) == public_id
    reader.read_session_observations.assert_not_awaited()
    async with async_sessionmaker(engine)() as session:
        row = await session.get(EventLog, public_id)
        assert row is not None
        assert await session.scalar(select(func.count()).select_from(EventLog)) == 1


async def test_open_refuses_a_runner_session_id(engine: AsyncEngine) -> None:
    await seed_retained_session(engine)
    reader = AsyncMock(spec=HistoryServiceClient)
    current = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    with pytest.raises(SessionAdmissionError, match="canonical"):
        await current.open("s-retained")


async def test_a_thread_exists_by_its_public_identity(engine: AsyncEngine) -> None:
    public_id = await seed_retained_session(engine)
    reader = AsyncMock(spec=HistoryServiceClient)
    current = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    assert await current.exists(public_id)
    assert not await current.exists(uuid4())


async def test_existing_identity_winning_registration_race_is_preserved(engine: AsyncEngine) -> None:
    public_id = uuid4()
    reader = AsyncMock(spec=HistoryServiceClient)

    async def register(session_id: str, *, limit: int) -> history_service_pb2.ReadSessionObservationsResponse:
        assert session_id == str(public_id)
        assert limit == 1
        await seed_retained_session(engine, public_id=public_id)
        return history_service_pb2.ReadSessionObservationsResponse(last_cursor=0)

    reader.read_session_observations.side_effect = register
    current = EventLogStore(
        engine,
        summaries=Summaries(engine, cast(HistoryServiceClient, reader)),
        history_reader=cast(HistoryServiceClient, reader),
    )
    assert await current.open(str(public_id)) == public_id
    async with async_sessionmaker(engine)() as session:
        row = await session.get(EventLog, public_id)
        assert row is not None


if __name__ == "__main__":
    pytest_bazel.main()
