import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import AsyncEngine

from devinfra.claude.session_export import store as store_module
from devinfra.claude.session_export.conftest import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_PAUSED,
    TEST_EPOCH,
    make_event,
    make_session,
)
from devinfra.claude.session_export.models import SESSION_STATUS_ARCHIVED, Event
from devinfra.claude.session_export.store import Base, SessionStore, StoreCounts, dumps_jsonb, make_engine


@pytest.mark.parametrize(
    ("document", "stored"),
    [
        ({"a": "x\x00y"}, {"a": "x␀y"}),
        ({"\x00": ["\x00", {"n": "\x00\x00"}]}, {"␀": ["␀", {"n": "␀␀"}]}),
        # A backslash before the text `u0000` is not a NUL escape, alone or before a real one.
        ({"a": "\\u0000", "b": "\\\\u0000"}, {"a": "\\u0000", "b": "\\\\u0000"}),
        ({"a": "\\\x00"}, {"a": "\\␀"}),
        ({"a": "héllo 😀"}, {"a": "héllo 😀"}),
    ],
)
def test_dumps_jsonb_rewrites_only_nul_characters(document: dict[str, Any], stored: dict[str, Any]) -> None:
    assert json.loads(dumps_jsonb(document)) == stored


def test_dumps_jsonb_logs_how_many_characters_it_rewrote(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=store_module.logger.name):
        dumps_jsonb({"a": "\x00\x00", "b": "\x00"})
    assert [record.getMessage() for record in caplog.records] == [
        "replaced 3 NUL character(s) with U+2400 in one JSON document"
    ]


async def test_only_recent_unarchived_sessions_are_worth_a_stream(store: SessionStore) -> None:
    since = TEST_EPOCH + timedelta(days=1)
    day = timedelta(days=1)
    await store.upsert_sessions(
        [
            make_session("session_recent0001", status=SESSION_STATUS_ACTIVE, last_event_at=(since + day).isoformat()),
            make_session(
                "session_recent0002", status=SESSION_STATUS_PAUSED, last_event_at=(since + 2 * day).isoformat()
            ),
            make_session(
                "session_recent0003", status=SESSION_STATUS_ACTIVE, last_event_at=(since + 3 * day).isoformat()
            ),
            make_session(
                "session_archived", status=SESSION_STATUS_ARCHIVED, last_event_at=(since + 4 * day).isoformat()
            ),
            make_session("session_stale", status=SESSION_STATUS_ACTIVE, last_event_at=(since - day).isoformat()),
        ]
    )
    assert await store.live_session_ids(active_since=since, limit=10) == [
        "session_recent0003",
        "session_recent0002",
        "session_recent0001",
    ]
    assert await store.live_session_ids(active_since=since, limit=2) == ["session_recent0003", "session_recent0002"]


async def test_behind_leaves_out_the_sessions_a_live_stream_keeps_current(store: SessionStore) -> None:
    await store.upsert_sessions([make_session("session_streamed"), make_session("session_polled")])  # neither is level
    assert await store.counts(followed=set()) == StoreCounts(sessions=2, behind=2)
    assert await store.counts(followed={"session_streamed"}) == StoreCounts(sessions=2, behind=1)


async def test_migration_matches_the_orm_metadata(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        drift = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    assert drift == []


async def test_session_change_journal_is_idempotent_and_covers_events(store: SessionStore) -> None:
    session = make_session("session_change0001")
    assert await store.current_change_revision() == 0

    await store.upsert_sessions([session])
    first = await store.changes_after(0)
    assert first.current_revision == 1
    assert first.changes[0].session_ids == (session.id,)

    await store.upsert_sessions([session])
    assert await store.current_change_revision() == 1

    event = Event.model_validate(make_event(1))
    await store.append_events(session.id, [event])
    second = await store.changes_after(1)
    assert second.current_revision == 2
    assert second.changes[0].session_ids == (session.id,)

    await store.append_events(session.id, [event])
    assert await store.current_change_revision() == 2


async def test_listener_on_another_engine_wakes_for_durable_changes(database_url: str, store: SessionStore) -> None:
    replica_engine = make_engine(database_url)
    replica = SessionStore(replica_engine)
    try:
        before = await replica.current_change_revision()
        async with replica.listen_for_changes() as notified:
            session = make_session("session_replica0001")
            await store.upsert_sessions([session])
            await asyncio.wait_for(notified.wait(), timeout=3)

        replay = await replica.changes_after(before)
        assert replay.changes[0].session_ids == (session.id,)
    finally:
        await replica_engine.dispose()


async def test_change_journal_reports_when_a_resume_token_has_expired(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "CHANGE_RETENTION_REVISIONS", 2)
    for index in range(4):
        await store.upsert_sessions([make_session(f"session_retained{index:04d}")])

    expired = await store.changes_after(0)
    assert expired.current_revision == 4
    assert expired.oldest_retained == 2
    assert [change.revision for change in expired.changes] == [3, 4]


if __name__ == "__main__":
    pytest_bazel.main()
