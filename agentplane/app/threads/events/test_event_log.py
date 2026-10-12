"""Production service-backed archive reads and retained identity compatibility."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.testing.retained_history import seed_retained_session
from agentplane.app.testing.thread_test_support import event_entry
from agentplane.app.threads.events.event_log import EventLogStore, SessionAdmissionError
from agentplane.app.threads.models import Thread
from agentplane.app.threads.summaries import Summaries
from agentplane.grpc_calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto import event_pb2, history_service_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

THREAD = UUID("00000000-0000-0000-0000-000000000001")


def store_over(engine: AsyncEngine, reader: object) -> EventLogStore:
    """The store reading its archive and summaries through `reader`."""
    client = cast(HistoryServiceClient, reader)
    return EventLogStore(engine, summaries=Summaries(client), history_reader=client)


@pytest.fixture
def reader() -> AsyncMock:
    return AsyncMock(spec=HistoryServiceClient)


@pytest.fixture
def remote(engine: AsyncEngine, reader: AsyncMock) -> EventLogStore:
    return store_over(engine, reader)


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
    remote = store_over(engine, reader)
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

    remote = store_over(engine, Reader())
    assert [e.cursor for e in await remote.events(THREAD, limit=10)] == [1, 2]
    assert calls == [0, 1]


# Cursors 3-5 were settled; 6 completes the item.
SETTLED = history_service_pb2.SettledDeltas(
    item_id="test-item", ranges=[history_service_pb2.CursorRange(first=3, last=5)]
)


async def test_service_read_walks_over_settled_ranges(engine: AsyncEngine) -> None:
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

    remote = store_over(engine, Reader())
    assert [entry.cursor for entry in await remote.events(THREAD, limit=10)] == [1, 2, 6, 7]
    assert await remote.observation_entry(THREAD, 4) is None
    observed = await remote.observation_entry(THREAD, 6)
    assert observed is not None
    assert observed.entry["cursor"] == "6"


@pytest.mark.parametrize("case", ["gap", "beyond_watermark", "resume_ahead"])
async def test_service_read_rejects_invalid_pages(engine: AsyncEngine, case: str) -> None:
    class Reader:
        async def read_session_events(
            self, session_id: str, *, after_cursor: int = 0, limit: int = 128
        ) -> history_service_pb2.ReadSessionEventsResponse:
            entries = [] if case != "beyond_watermark" else [event_entry(1)]
            return history_service_pb2.ReadSessionEventsResponse(last_cursor=1 if case == "gap" else 0, entries=entries)

    remote = store_over(engine, Reader())
    with pytest.raises(ConnectionError):
        await remote.events(THREAD, after_cursor=1 if case == "resume_ahead" else 0, limit=1)


async def test_concurrent_replicas_create_one_service_thread(engine: AsyncEngine, reader: AsyncMock) -> None:
    stores = [store_over(engine, reader) for _ in range(2)]
    public_id = uuid4()
    first, second = await asyncio.gather(*(store.open(str(public_id)) for store in stores))
    assert first == second == public_id
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(Thread)) == 1


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
    reader: AsyncMock,
    remote: EventLogStore,
    before: int | None,
    after: int | None,
    cursors: list[int],
    older: str | None,
    newer: str | None,
) -> None:
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(
        last_cursor=5,
        observations=[history_service_pb2.SessionObservation(cursor=cursor, kind="native") for cursor in cursors],
    )
    page = await remote.observations(THREAD, before_cursor=before, after_cursor=after, limit=2)
    assert [row.cursor for row in page.observations] == [str(cursor) for cursor in cursors]
    assert page.next_before_cursor == older
    assert page.next_after_cursor == newer
    reader.read_session_events.assert_not_awaited()
    reader.read_session_observations.assert_awaited_once_with(
        str(THREAD), before_cursor=before, after_cursor=after, limit=2
    )


@pytest.mark.parametrize(
    ("before", "after", "cursors"),
    [(None, None, [6, 7]), (7, None, [2, 6]), (5, None, [1, 2]), (None, 2, [6, 7]), (None, 6, [7])],
)
async def test_service_observation_pages_treat_settled_ranges_as_covered(
    reader: AsyncMock, remote: EventLogStore, before: int | None, after: int | None, cursors: list[int]
) -> None:
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(
        last_cursor=7,
        observations=[history_service_pb2.SessionObservation(cursor=cursor, kind="native") for cursor in cursors],
        settled=SETTLED.ranges,
    )
    page = await remote.observations(THREAD, before_cursor=before, after_cursor=after, limit=2)
    assert [row.cursor for row in page.observations] == [str(cursor) for cursor in cursors]


async def test_service_observation_page_rejects_missing_entries(reader: AsyncMock, remote: EventLogStore) -> None:
    reader.read_session_observations.return_value = history_service_pb2.ReadSessionObservationsResponse(last_cursor=5)
    with pytest.raises(ConnectionError, match="invalid service observation"):
        await remote.observations(THREAD, limit=2)


async def test_a_session_the_history_service_has_not_recorded_opens_with_empty_history(
    reader: AsyncMock, remote: EventLogStore
) -> None:
    # The runner pushes a new Session to the History Service after OpenSession has answered.
    reader.read_session_events.side_effect = ServiceError(grpc.StatusCode.NOT_FOUND, "test-unrecorded-session")
    reader.read_session_observations.side_effect = ServiceError(grpc.StatusCode.NOT_FOUND, "test-unrecorded-session")
    thread_id = await remote.open(str(uuid4()))
    assert await remote.exists(thread_id)
    assert await remote.events(thread_id, limit=10) == []
    assert await remote.read_watermark(thread_id) == 0
    reader.read_session_events.side_effect = ServiceError(grpc.StatusCode.UNAVAILABLE, "test-history-down")
    with pytest.raises(ServiceError):
        await remote.events(thread_id, limit=10)


async def test_public_session_open_preserves_retained_identity(
    engine: AsyncEngine, reader: AsyncMock, remote: EventLogStore
) -> None:
    public_id = await seed_retained_session(engine)
    assert await remote.open(str(public_id)) == public_id
    reader.read_session_observations.assert_not_awaited()
    async with async_sessionmaker(engine)() as session:
        row = await session.get(Thread, public_id)
        assert row is not None
        assert await session.scalar(select(func.count()).select_from(Thread)) == 1


async def test_open_refuses_a_runner_session_id(engine: AsyncEngine, remote: EventLogStore) -> None:
    await seed_retained_session(engine)
    with pytest.raises(SessionAdmissionError, match="canonical"):
        await remote.open("s-retained")


async def test_a_thread_exists_by_its_public_identity(engine: AsyncEngine, remote: EventLogStore) -> None:
    public_id = await seed_retained_session(engine)
    assert await remote.exists(public_id)
    assert not await remote.exists(uuid4())


if __name__ == "__main__":
    pytest_bazel.main()
