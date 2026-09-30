import httpx
import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.conftest import FakeSessionsService, make_event, make_events
from devinfra.claude.session_export.models import canonical_id, parse_timestamp
from devinfra.claude.session_export.store import Base, EventRow, SessionRow, SessionStore
from devinfra.claude.session_export.sync import sync_once, sync_session

ONE, TWO, EMPTY = "session_test0001", "session_test0002", "session_test0003"


def event_reads(service: FakeSessionsService) -> list[httpx.Request]:
    """Oldest-first reads of events, not the newest-sequence probes."""
    return [r for r in service.requests if r.url.path.endswith("/events") and r.url.params["sort_order"] == "asc"]


async def event_counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as connection:
        rows = await connection.execute(select(EventRow.session_id, func.count()).group_by(EventRow.session_id))
        return dict(rows.tuples().all())


async def test_sync_backfills_every_session_then_reads_only_what_changed(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine
) -> None:
    service.events = {ONE: make_events(3), TWO: make_events(700), EMPTY: []}
    await sync_once(api, store, workers=2)
    assert await event_counts(engine) == {ONE: 3, TWO: 700}
    level = {i: parse_timestamp(service.list_item(i)["last_event_at"]) for i in service.events}
    assert await store.synced_last_event_at() == level

    service.requests.clear()
    await sync_once(api, store, workers=2)
    assert event_reads(service) == []

    service.events[ONE] += [make_event(4), make_event(5)]
    await sync_once(api, store, workers=2)
    assert await event_counts(engine) == {ONE: 5, TWO: 700}
    assert [(r.url.path, r.url.params["cursor"]) for r in event_reads(service)] == [
        (f"/v1/code/sessions/{ONE}/events", "3")
    ]


async def test_sync_refreshes_session_metadata_without_rereading_events(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine
) -> None:
    service.events = {ONE: make_events(3)}
    await sync_once(api, store, workers=1)
    service.titles[ONE] = "Renamed"
    service.requests.clear()
    await sync_once(api, store, workers=1)
    async with engine.connect() as connection:
        assert (await connection.execute(select(SessionRow.title, SessionRow.raw["title"].as_string()))).one() == (
            "Renamed",
            "Renamed",
        )
    assert event_reads(service) == []


async def test_sync_stores_nul_as_the_symbol_for_null(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine
) -> None:
    service.events = {ONE: [make_event(1, payload={"text": "a\x00b", "literal": "\\u0000"})]}
    await sync_once(api, store, workers=1)
    async with engine.connect() as connection:
        assert (await connection.execute(select(EventRow.payload))).scalar_one() == {
            "text": "a␀b",
            "literal": "\\u0000",
        }


async def test_sync_refreshes_the_stamps_of_events_the_worker_had_not_finished(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore, engine: AsyncEngine
) -> None:
    received, processing, processed = (f"2026-02-01T00:0{n}:00+00:00" for n in (1, 2, 3))
    events = make_events(3)
    events[1] = make_event(2, received_at=received)
    service.events = {ONE: events}
    await sync_once(api, store, workers=1)

    service.requests.clear()
    events[1] = make_event(2, received_at=received, processing_at=processing, processed_at=processed)
    events.append(make_event(4))
    await sync_once(api, store, workers=1)
    async with engine.connect() as connection:
        rows = await connection.execute(select(EventRow.sequence_num, EventRow.processed_at))
        assert dict(rows.tuples().all()) == {1: None, 2: parse_timestamp(processed), 3: None, 4: None}
    assert [r.url.params["cursor"] for r in event_reads(service)] == ["1"]  # from the unfinished event, not event 3


async def test_sync_refuses_an_event_whose_unstored_fields_carry_data(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore
) -> None:
    service.events = {ONE: [make_event(1), make_event(2, sent_by_account_id="test-account")]}
    [session] = [s async for s in api.list_sessions()]
    await store.upsert_sessions([session])
    with pytest.raises(ValueError, match="sent_by_account_id"):
        await sync_session(api, store, session)
    assert await store.resume_after(canonical_id(session.id)) == 0
    assert await store.synced_last_event_at() == {ONE: None}


async def test_sync_stores_nothing_of_a_page_with_a_sequence_gap(
    service: FakeSessionsService, api: SessionsApi, store: SessionStore
) -> None:
    events = make_events(5)
    del events[2]
    service.events = {ONE: events}
    [session] = [s async for s in api.list_sessions()]
    await store.upsert_sessions([session])
    with pytest.raises(ValueError, match="sequence_num"):
        await sync_session(api, store, session)
    assert await store.resume_after(ONE) == 0


async def test_migration_matches_the_orm_metadata(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        drift = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    assert drift == []


if __name__ == "__main__":
    pytest_bazel.main()
