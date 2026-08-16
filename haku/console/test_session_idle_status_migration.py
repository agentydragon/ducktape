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


def _insert_session(conn: Connection, status: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, surface, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, 'spa', :status, :fingerprint, :n, :n, :n)
            """
        ),
        {
            "session_id": uuid4(),
            "operator_id": _operator(conn),
            "status": status,
            "fingerprint": b"fingerprint",
            "n": _NOW,
        },
    )


def test_idle_is_what_the_widening_adds(db_url: str, engine: Engine) -> None:
    apply_migrations(db_url, "0052")
    with engine.begin() as conn, pytest.raises(IntegrityError, match="ck_sessions_status"):
        _insert_session(conn, "idle")

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
