"""0050 adds the runner's frame number without changing what an existing writer may do."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)


def _session(conn: Connection) -> UUID:
    operator_id, session_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, surface, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, 'spa', 'ready', :fingerprint, :n, :n, :n)
            """
        ),
        {"session_id": session_id, "operator_id": operator_id, "fingerprint": b"digest", "n": _NOW},
    )
    return session_id


def _frame(conn: Connection, session_id: UUID, kind: str, **extra: object) -> None:
    columns = {"session_id": session_id, "direction": "from_agent", "kind": kind, "payload": "{}", **extra}
    named = ", ".join(columns)
    conn.execute(
        text(
            f"INSERT INTO session_frames ({named}, partial, created_at, updated_at) "
            f"VALUES ({', '.join(f':{name}' for name in columns)}, false, :n, :n)"
        ),
        columns | {"n": _NOW},
    )


def test_a_writer_that_never_heard_of_the_column_still_records_frames(db_url: str) -> None:
    """The roll: `maxUnavailable: 0` means a replica on the previous image keeps recording frames
    against this schema, and its INSERT names every column that image knew and no more. Nullable is
    what makes that legal, and NULL is the honest answer — nothing numbered that frame."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            _frame(conn, session_id, "assistant")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT runner_seq FROM session_frames")).scalar_one() is None
    finally:
        engine.dispose()


def test_one_session_may_hold_the_same_runner_number_twice(db_url: str) -> None:
    """The index is deliberately not unique while dedup still keys on `frame_uid`.

    Postgres infers one conflict target, so uniqueness here would turn a replayed frame with no
    agent-assigned identity — a `control_response`, a `system` with no `task_id` — from one
    duplicate row into a violation raised inside the reader, ending the session. That is the
    behaviour this pins until dedup keys on position
    (<../plans/chat_runtime_projection.md> § 2b, R4).
    """
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn)
            _frame(conn, session_id, "control_response", runner_seq=7)
            _frame(conn, session_id, "control_response", runner_seq=7)

        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM session_frames WHERE runner_seq = 7")).scalar_one() == 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
