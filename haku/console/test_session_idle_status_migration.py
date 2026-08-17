"""0054 widens `ck_sessions_status` to admit `idle` — the schema half of a session that holds no
sandbox, shipped a release before anything writes it."""

from __future__ import annotations

import datetime
from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)
_STATUSES_BEFORE = ("provisioning", "ready", "responding", "closing", "closed", "failed")


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


def _insert_session(conn: Connection, status: str, *, threaded: bool = True) -> None:
    """*threaded* is False for a revision older than `0064`, which is where `conversation` and the
    column pointing at it begin; from `0072` on, naming it is the only way to insert a session."""
    operator_id = _operator(conn)
    columns: dict[str, object] = {
        "session_id": uuid4(),
        "operator_id": operator_id,
        "surface": "spa",
        "status": status,
        "bridge_token_fingerprint": b"fingerprint",
    }
    if threaded:
        conversation_id = uuid4()
        conn.execute(
            text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :o, :n)"),
            {"id": conversation_id, "o": operator_id, "n": _NOW},
        )
        columns["conversation_id"] = conversation_id
    named = ", ".join(columns)
    conn.execute(
        text(
            f"INSERT INTO sessions ({named}, lease_expires_at, created_at, updated_at) "
            f"VALUES ({', '.join(f':{name}' for name in columns)}, :n, :n, :n)"
        ),
        columns | {"n": _NOW},
    )


def test_idle_is_what_the_widening_adds(db_url: str, engine: Engine) -> None:
    apply_migrations(db_url, "0052")
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_sessions_status"):
        _insert_session(conn, "idle", threaded=False)

    apply_migrations(db_url)
    with engine.begin() as conn:
        _insert_session(conn, "idle")


@pytest.mark.parametrize("status", _STATUSES_BEFORE)
def test_a_replica_on_the_previous_image_still_writes_every_status_it_knows(
    db_url: str, engine: Engine, status: str
) -> None:
    """The roll's other direction: widening admits a value, it must retract none."""
    apply_migrations(db_url)
    with engine.begin() as conn:
        _insert_session(conn, status)


def test_the_widening_is_one_member_rather_than_a_hole(db_url: str, engine: Engine) -> None:
    apply_migrations(db_url)
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_sessions_status"):
        _insert_session(conn, "sleeping")


if __name__ == "__main__":
    pytest_bazel.main()
