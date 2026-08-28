"""0109 arms the generation cut: it closes legacy sessions and refuses to apply while one is live.

The refusal is the safety the whole maintenance window rests on (#4667 comment 5422375226): merging
the migration is scheduling the window, and it must fail the deploy rather than cut over a running
system. Pins the assert-and-reject, the non-launchable close of the drained remainder, and the
frames-constraint relaxation the runner's two-direction numbering needs.

Temporary per `AGENTS.md` § "Do not keep tests for old migrations": delete once the chain is roughly
five revisions past 0109.
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC)


def _conversation(conn: Connection) -> tuple[UUID, UUID]:
    operator_id, conversation_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, runtime_kind, created_at)"
            " VALUES (:id, :operator_id, 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    return operator_id, conversation_id


def _session(conn: Connection, *, live: bool) -> None:
    operator_id, conversation_id = _conversation(conn)
    # A live session holds a claimed sandbox (bridge fingerprint + lease) and has not ended; an
    # idle one holds neither. Only the live one must block the cut.
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:session_id, :operator_id, :conversation_id, :fingerprint, :lease, :n, :n)
            """
        ),
        {
            "session_id": uuid4(),
            "operator_id": operator_id,
            "conversation_id": conversation_id,
            "fingerprint": uuid4().bytes if live else None,
            "lease": _NOW if live else None,
            "n": _NOW,
        },
    )


def _frames_direction_constraint(conn: Connection) -> bool:
    count: int = conn.execute(
        text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_session_frames_runner_seq_direction'")
    ).scalar_one()
    return count == 1


def test_0109_cuts_a_drained_database(db_url: str) -> None:
    apply_migrations(db_url, "0108")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _session(conn, live=False)  # an idle session does not block the cut
        apply_migrations(db_url)
        with engine.begin() as conn:
            # The idle session is closed — non-launchable — while its history stays readable.
            assert conn.execute(text("SELECT count(*) FROM sessions WHERE ended_at IS NULL")).scalar_one() == 0
            # A runner number rides both directions now, so the v3 direction constraint is gone.
            assert not _frames_direction_constraint(conn)
    finally:
        engine.dispose()


def test_0109_refuses_to_cut_while_a_session_is_live(db_url: str) -> None:
    apply_migrations(db_url, "0108")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _session(conn, live=True)
        with pytest.raises(Exception, match=r"not drained|live session"):
            apply_migrations(db_url)
        # Nothing changed: the session is still live, and the v3 frames constraint still stands.
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM sessions WHERE ended_at IS NULL")).scalar_one() == 1
            assert _frames_direction_constraint(conn)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
