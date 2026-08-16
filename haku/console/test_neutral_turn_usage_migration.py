"""0049 turns one CLI's `usage` object into columns that mean the same thing on every backend."""

from __future__ import annotations

import datetime
import json
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)


def _operator(conn: Connection) -> UUID:
    operator_id = uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    return operator_id


def _session(conn: Connection, operator_id: UUID) -> UUID:
    session_id = uuid4()
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


def _turn(conn: Connection, session_id: UUID, *, usage: dict[str, int] | None, cost: float | None) -> UUID:
    turn_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO session_turns (
                turn_id, session_id, first_frame_seq, started_at, ended_at, outcome,
                cost_usd, usage, duration_ms
            ) VALUES (:turn_id, :session_id, 1, :n, :n, 'answered', :cost, :usage, 1200)
            """
        ),
        {
            "turn_id": turn_id,
            "session_id": session_id,
            "n": _NOW,
            "cost": cost,
            "usage": None if usage is None else json.dumps(usage),
        },
    )
    return turn_id


def test_the_backfill_reads_the_payload_the_columns_replace(db_url: str) -> None:
    """Exact rather than archaeological: the JSONB being dropped is where the numbers were, and
    `cache_read_input_tokens` is the key Claude spells the cached counter with. A counter the
    payload never carried is 0 — the neutral shape's own reading of an unreported one — and a row
    with a cost but no usage object still gets counters, so its cost survives the reader's test for
    whether an exchange was accounted for at all."""
    apply_migrations(db_url, "0048")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            session_id = _session(conn, _operator(conn))
            counted = _turn(
                conn,
                session_id,
                usage={"input_tokens": 19, "output_tokens": 1204, "cache_read_input_tokens": 133_907},
                cost=0.4213,
            )
            partial = _turn(conn, session_id, usage={"output_tokens": 7}, cost=None)
            cost_only = _turn(conn, session_id, usage=None, cost=0.5)
            open_turn = uuid4()
            conn.execute(
                text(
                    "INSERT INTO session_turns (turn_id, session_id, first_frame_seq, started_at) "
                    "VALUES (:turn_id, :session_id, 1, :n)"
                ),
                {"turn_id": open_turn, "session_id": session_id, "n": _NOW},
            )

        apply_migrations(db_url)

        with engine.connect() as conn:
            counters = {
                row.turn_id: (row.input_tokens, row.output_tokens, row.cached_input_tokens)
                for row in conn.execute(
                    text("SELECT turn_id, input_tokens, output_tokens, cached_input_tokens FROM session_turns")
                )
            }
        assert counters[counted] == (19, 1204, 133_907)
        assert counters[partial] == (0, 7, 0)
        assert counters[cost_only] == (0, 0, 0)
        assert counters[open_turn] == (None, None, None), "an exchange still running accounted for nothing yet"
    finally:
        engine.dispose()


def test_a_turn_closed_by_a_replica_that_never_heard_of_the_columns_is_still_legal(db_url: str) -> None:
    """The roll's other direction. The previous image writes `usage`, `cost_usd` and `duration_ms`
    and names no counter, so the constraint that keeps the three together must read that as "no
    usage reported" rather than refusing the write — which would fail turns for the length of a
    roll."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            turn_id = _turn(conn, _session(conn, _operator(conn)), usage={"output_tokens": 91}, cost=0.0125)

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT input_tokens, output_tokens, cached_input_tokens FROM session_turns WHERE turn_id = :id"),
                {"id": turn_id},
            ).one() == (None, None, None)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
