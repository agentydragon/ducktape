"""What `ck_sessions_status` admits: every status a replica writes, and not `idle`."""

from __future__ import annotations

import datetime
from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 18, tzinfo=datetime.UTC)
_STATUSES = ("provisioning", "ready", "responding", "closing", "closed", "failed")


@pytest.fixture
def engine(db_url: str) -> Generator[Engine]:
    created = create_engine(sync_database_url(db_url))
    try:
        yield created
    finally:
        created.dispose()


def _operator(conn: Connection) -> UUID:
    operator_id = uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    return operator_id


def _insert_session(conn: Connection, status: str) -> None:
    operator_id, conversation_id = _operator(conn), uuid4()
    conn.execute(
        text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :o, :n)"),
        {"id": conversation_id, "o": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            "INSERT INTO sessions (session_id, operator_id, conversation_id, status, bridge_token_fingerprint, "
            "lease_expires_at, created_at, updated_at) "
            "VALUES (:session_id, :operator_id, :conversation_id, :status, :fingerprint, :n, :n, :n)"
        ),
        {
            "session_id": uuid4(),
            "operator_id": operator_id,
            "conversation_id": conversation_id,
            "status": status,
            "fingerprint": b"fingerprint",
            "n": _NOW,
        },
    )


def test_idle_is_what_the_narrowing_takes_away(db_url: str, engine: Engine) -> None:
    apply_migrations(db_url)
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_sessions_status"):
        _insert_session(conn, "idle")


@pytest.mark.parametrize("status", _STATUSES)
def test_a_replica_on_the_previous_image_still_writes_every_status_it_knows(
    db_url: str, engine: Engine, status: str
) -> None:
    """The roll's other direction, and the whole safety argument: the previous image keeps serving
    against this schema, so a narrowing must reject only what nothing writes."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        _insert_session(conn, status)


if __name__ == "__main__":
    pytest_bazel.main()
