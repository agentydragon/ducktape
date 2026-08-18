"""`sessions.claim_cleaned_at` carries the cleanup fact, and a writer may leave it unset."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)


def _session(
    conn: Connection, operator_id: UUID, *, status: str, fingerprint: bytes, updated_at: datetime.datetime
) -> UUID:
    session_id, conversation_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO conversation (conversation_id, operator_id, created_at) VALUES (:id, :o, :n)"),
        {"id": conversation_id, "o": operator_id, "n": _NOW},
    )
    columns: dict[str, object] = {
        "session_id": session_id,
        "operator_id": operator_id,
        "conversation_id": conversation_id,
        "status": status,
        "bridge_token_fingerprint": fingerprint,
    }
    named = ", ".join(columns)
    conn.execute(
        text(
            f"INSERT INTO sessions ({named}, lease_expires_at, created_at, updated_at) "
            f"VALUES ({', '.join(f':{name}' for name in columns)}, :n, :n, :updated_at)"
        ),
        columns | {"n": _NOW, "updated_at": updated_at},
    )
    return session_id


def test_a_row_written_by_a_replica_that_never_heard_of_the_column_is_still_legal(db_url: str) -> None:
    """The roll's other direction: an INSERT naming every column the previous image knew must still
    succeed, which is what nullable buys — and such a row is honestly cleanup-pending."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    operator_id = uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"
                ),
                {"id": operator_id, "n": _NOW},
            )
            session_id = _session(conn, operator_id, status="ready", fingerprint=b"digest", updated_at=_NOW)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT claim_cleaned_at FROM sessions WHERE session_id = :id"), {"id": session_id}
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
