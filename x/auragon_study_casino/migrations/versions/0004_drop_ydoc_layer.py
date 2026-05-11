"""Drop the Y.Doc layer; canonical state moves to relational tables.

Revision ID: 0004
Revises: 0003

Backfill: every existing `doc.update_blob` is decoded with pycrdt and its
balance / sessions / prizes / prize_log fanned out into the four new
tables. After the backfill, `state_snapshots.doc_update_blob` and the
`doc` table itself are dropped — the application no longer reads either.

This is the only post-Stage-3 consumer of pycrdt. After 0004 has run on
production, pycrdt can be dropped from the runtime image (a follow-up
commit on the same branch removes it from `requirements_bazel.txt`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pycrdt import Array, Doc, Map

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Default prize catalog seeded into a fresh DB. Mirrors what the pre-0004
# DocStore seeded into the Y.Doc on first boot. Kept here (and not in
# doc_shape.py, which 0004's parent commit deletes) so the migration is
# self-contained.
_DEFAULT_PRIZES: list[tuple[str, str, int]] = [
    ("p1", "Anime episode break", 30),
    ("p2", "Nice coffee shop trip", 60),
    ("p3", "Takeout night", 120),
    ("p4", "Nice dinner out with Rai", 240),
    ("p5", "Buy a new game", 600),
    ("p6", "Weekend getaway", 1800),
]


def _decode_blob(blob: bytes) -> dict[str, Any]:
    """Decode a Y.Doc binary update into the same JSON shape `state_snapshots.decoded_json` uses."""
    doc = Doc()
    balance: Map = doc.get("balance", type=Map)
    sessions: Map = doc.get("sessions", type=Map)
    prizes: Map = doc.get("prizes", type=Map)
    prize_log: Array = doc.get("prize_log", type=Array)
    doc.apply_update(blob)

    decoded_sessions = []
    for session_id, session in sessions.items():
        ended_at_ms = session.get("ended_at_ms")
        # In-progress sessions (no ended_at_ms) lived on the client side
        # post-Stage-3, but a few stale Y.Docs may still carry one. Skip them
        # — the 0004 cutover moves active-session state to localStorage; any
        # leftover server-side in-progress entry is discarded.
        if ended_at_ms is None:
            continue
        decoded_sessions.append(
            {
                "id": str(session_id),
                "subject": str(session.get("subject") or ""),
                "seconds": int(session.get("seconds") or 0),
                "ended_at_ms": int(ended_at_ms),
            }
        )

    decoded_prizes = [
        {"id": str(prize_id), "name": str(prize.get("name") or ""), "cost": int(prize.get("cost") or 0)}
        for prize_id, prize in prizes.items()
    ]

    decoded_prize_log = [
        {
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            "cost": int(entry.get("cost") or 0),
            "at_ms": int(entry.get("at_ms") or 0),
        }
        for entry in prize_log
    ]

    return {
        "balance": {"credits": int(balance.get("credits") or 0), "tokens": int(balance.get("tokens") or 0)},
        "sessions": decoded_sessions,
        "prizes": decoded_prizes,
        "prize_log": decoded_prize_log,
    }


def upgrade() -> None:
    bind = op.get_bind()

    # 1) Create the new relational tables.
    op.create_table(
        "balance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="balance_single_row"),
        sa.CheckConstraint("credits >= 0", name="balance_credits_nonneg"),
        sa.CheckConstraint("tokens >= 0", name="balance_tokens_nonneg"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("subject", sa.String(length=120), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("ended_at_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("seconds >= 0", name="sessions_seconds_nonneg"),
        sa.CheckConstraint("length(subject) > 0", name="sessions_subject_nonempty"),
    )
    op.create_index("idx_sessions_ended_at", "sessions", ["ended_at_ms"])
    op.create_table(
        "prizes",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.CheckConstraint("cost > 0", name="prizes_cost_positive"),
        sa.CheckConstraint("length(name) > 0", name="prizes_name_nonempty"),
    )
    op.create_table(
        "prize_log",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.Column("at_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("cost >= 0", name="prize_log_cost_nonneg"),
    )
    op.create_index("idx_prize_log_at_ms", "prize_log", ["at_ms"])

    # 2) Backfill from the existing doc.update_blob, if any.
    doc_row = bind.execute(sa.text("SELECT update_blob FROM doc WHERE id = 1")).fetchone()
    if doc_row is None:
        # Fresh DB — seed defaults so the app starts with a usable prize catalog.
        bind.execute(sa.text("INSERT INTO balance (id, credits, tokens) VALUES (1, 0, 0)"))
        for prize_id, name, cost in _DEFAULT_PRIZES:
            bind.execute(
                sa.text("INSERT INTO prizes (id, name, cost) VALUES (:id, :name, :cost)"),
                {"id": prize_id, "name": name, "cost": cost},
            )
    else:
        decoded = _decode_blob(doc_row[0])
        bind.execute(
            sa.text("INSERT INTO balance (id, credits, tokens) VALUES (1, :credits, :tokens)"),
            {"credits": decoded["balance"]["credits"], "tokens": decoded["balance"]["tokens"]},
        )
        for session in decoded["sessions"]:
            bind.execute(
                sa.text(
                    "INSERT INTO sessions (id, subject, seconds, ended_at_ms) "
                    "VALUES (:id, :subject, :seconds, :ended_at_ms)"
                ),
                session,
            )
        for prize in decoded["prizes"]:
            bind.execute(sa.text("INSERT INTO prizes (id, name, cost) VALUES (:id, :name, :cost)"), prize)
        for entry in decoded["prize_log"]:
            bind.execute(
                sa.text("INSERT INTO prize_log (id, name, cost, at_ms) VALUES (:id, :name, :cost, :at_ms)"), entry
            )

    # 3) Drop server_default on balance now that the canonical row exists; future
    #    inserts go through the ORM which sets explicit values.
    with op.batch_alter_table("balance") as batch:
        batch.alter_column("credits", server_default=None)
        batch.alter_column("tokens", server_default=None)

    # 4) Re-snapshot decoded_json for any existing state_snapshots whose stored
    #    decoded_json predates the relational shape — they used the same JSON
    #    layout already (see store.py:_casino_json), so this is a no-op for
    #    in-place schema; we only need to drop the now-unused blob column.
    with op.batch_alter_table("state_snapshots") as batch:
        batch.drop_column("doc_update_blob")

    # 5) Drop the legacy doc table.
    op.drop_table("doc")


def downgrade() -> None:
    # The relational backfill is irreversible without re-encoding state into a
    # fresh Y.Doc binary update — out of scope for this migration. Refuse.
    raise NotImplementedError("0004 is one-way; recover from a `state_snapshots.decoded_json` row instead")
