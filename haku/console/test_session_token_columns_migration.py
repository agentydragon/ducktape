"""The 0122 expand copies the bridge-named session columns into their session-token names.

Landing-window coverage per AGENTS.md § old-migration tests: delete roughly five revisions after
0122 lands (the current-schema equivalence tests keep guarding the end state).
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 29, tzinfo=datetime.UTC)


def _session(conn: Connection, *, fingerprint: bytes | None, connected: bool) -> UUID:
    operator_id, conversation_id, session_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, harness_kind, runtime_kind, created_at) "
            "VALUES (:id, :operator_id, 'claude_code', 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, bridge_token_fingerprint,
                bridge_connected_at, lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, :conversation_id, :fingerprint, :connected_at, :lease, :n, :n)
            """
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "conversation_id": conversation_id,
            "fingerprint": fingerprint,
            "connected_at": _NOW if connected else None,
            "lease": _NOW if fingerprint is not None else None,
            "n": _NOW,
        },
    )
    return session_id


def test_expand_backfills_the_session_token_columns_from_the_bridge_columns(db_url: str) -> None:
    apply_migrations(db_url, "0119")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            connected = _session(conn, fingerprint=b"digest-a", connected=True)
            allocated = _session(conn, fingerprint=b"digest-b", connected=False)
            idle = _session(conn, fingerprint=None, connected=False)

        apply_migrations(db_url, "0122")

        with engine.connect() as conn:
            rows = {
                row.session_id: row
                for row in conn.execute(
                    text(
                        "SELECT session_id, session_token_fingerprint, runner_connected_at, "
                        "bridge_token_fingerprint, bridge_connected_at FROM sessions"
                    )
                )
            }
        for session_id in (connected, allocated, idle):
            row = rows[session_id]
            assert row.session_token_fingerprint == row.bridge_token_fingerprint
            assert row.runner_connected_at == row.bridge_connected_at
        assert rows[connected].session_token_fingerprint == b"digest-a"
        assert rows[connected].runner_connected_at is not None
        assert rows[idle].session_token_fingerprint is None
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
