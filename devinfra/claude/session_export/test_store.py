import json
import logging
from datetime import timedelta
from typing import Any

import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import AsyncEngine

from devinfra.claude.session_export import store
from devinfra.claude.session_export.conftest import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_PAUSED,
    TEST_EPOCH,
    make_session,
)
from devinfra.claude.session_export.models import SESSION_STATUS_ARCHIVED
from devinfra.claude.session_export.store import Base, SessionStore, dumps_jsonb


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
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
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


async def test_migration_matches_the_orm_metadata(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        drift = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    assert drift == []


if __name__ == "__main__":
    pytest_bazel.main()
