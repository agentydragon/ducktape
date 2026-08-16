"""0048 moves the cleanup fact off the credential column without re-offering settled sessions."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)
_CLEANED_AT = datetime.datetime(2026, 8, 14, 9, 30, tzinfo=datetime.UTC)


def _session(
    conn: Connection, operator_id: UUID, *, status: str, fingerprint: bytes, updated_at: datetime.datetime
) -> UUID:
    session_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, surface, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, 'spa', :status, :fingerprint, :n, :n, :updated_at)
            """
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "status": status,
            "fingerprint": fingerprint,
            "n": _NOW,
            "updated_at": updated_at,
        },
    )
    return session_id


def test_the_backfill_settles_already_cleaned_rows_and_leaves_the_rest_pending(db_url: str) -> None:
    """The sweep's predicate changes column, not meaning: a session whose claim the previous image
    already deleted must not come back as a candidate, and one whose claim is still out there must.
    """
    apply_migrations(db_url, "0047")
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
            cleaned = _session(conn, operator_id, status="closed", fingerprint=b"", updated_at=_CLEANED_AT)
            pending = _session(conn, operator_id, status="failed", fingerprint=b"digest", updated_at=_NOW)
            live = _session(conn, operator_id, status="ready", fingerprint=b"digest", updated_at=_NOW)

        apply_migrations(db_url)

        with engine.connect() as conn:
            stamps = {
                row.session_id: row.claim_cleaned_at
                for row in conn.execute(text("SELECT session_id, claim_cleaned_at FROM sessions"))
            }
            assert stamps[cleaned] == _CLEANED_AT, "the recorded cleanup instant, not this migration's wall clock"
            assert stamps[pending] is None
            assert stamps[live] is None
            # The credential column is not what the backfill read out of — it is left exactly as it
            # was, including the blank an old replica wrote.
            assert (
                conn.execute(
                    text("SELECT bridge_token_fingerprint FROM sessions WHERE session_id = :id"), {"id": cleaned}
                ).scalar_one()
                == b""
            )
    finally:
        engine.dispose()


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
