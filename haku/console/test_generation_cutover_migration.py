"""0109 arms the generation cut: it sets the generation, lands admission open, and refuses to
apply while a session is live.

The refusal is the safety the whole maintenance window rests on (#4667 comment 5422375226): merging
the migration is scheduling the window, and it must fail the deploy rather than cut over a running
system. Pins the assert-and-reject, the generation value against the code constant a runner is
admitted against, and the open-admission landing state.

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
from haku.runtime.x.bridge.neutral_operations import GENERATION

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


def test_0109_cuts_a_drained_database_and_lands_admission_open(db_url: str) -> None:
    apply_migrations(db_url, "0108")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _session(conn, live=False)  # an idle session does not block the cut
        apply_migrations(db_url)
        with engine.begin() as conn:
            row = conn.execute(text("SELECT generation, admission_closed FROM runtime_control WHERE id = 1")).one()
            # The generation the migration set is the exact value a runner is admitted against; the
            # cut is coherent only if the two agree. Admission lands open — the peering gate is the
            # cut's safety, and the operator closes admission through the API for the gate window.
            assert row.generation == GENERATION
            assert row.admission_closed is False
            # The idle session is closed — non-launchable — while its history stays readable.
            assert conn.execute(text("SELECT count(*) FROM sessions WHERE ended_at IS NULL")).scalar_one() == 0
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
        # Nothing changed: the switch table was never created, so admission is not even a concept yet.
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('public.runtime_control')")).scalar_one()
            assert exists is None
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
