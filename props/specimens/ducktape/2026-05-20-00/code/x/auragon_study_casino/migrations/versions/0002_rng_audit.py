"""Persist deterministic RNG audit traces.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rng_action_audits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("client_action_id", sa.String(length=128), nullable=False),
        sa.Column("ledger_event_id", sa.Integer(), nullable=False),
        sa.Column("game_event_id", sa.Integer(), nullable=True),
        sa.Column("server_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("rng_version", sa.String(length=32), nullable=False),
        sa.Column("rng_key_id", sa.String(length=64), nullable=False),
        sa.Column("seed_material_json", sa.Text(), nullable=False),
        sa.Column("seed_digest_hex", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["ledger_event_id"], ["ledger_events.id"]),
        sa.ForeignKeyConstraint(["game_event_id"], ["game_events.id"]),
        sa.UniqueConstraint("user_id", "client_action_id", name="rng_action_audits_user_client_action_id_unique"),
    )
    op.create_index("idx_rng_action_audits_user_id", "rng_action_audits", ["user_id", "id"])
    op.create_index("idx_rng_action_audits_ledger_event_id", "rng_action_audits", ["ledger_event_id"])

    op.create_table(
        "rng_call_audits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action_audit_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("client_action_id", sa.String(length=128), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=128), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["action_audit_id"], ["rng_action_audits.id"]),
        sa.UniqueConstraint("action_audit_id", "call_index", name="rng_call_audits_action_call_unique"),
    )
    op.create_index("idx_rng_call_audits_action_id", "rng_call_audits", ["action_audit_id", "call_index"])
    op.create_index("idx_rng_call_audits_user_action", "rng_call_audits", ["user_id", "client_action_id", "call_index"])


def downgrade() -> None:
    op.drop_index("idx_rng_call_audits_user_action", table_name="rng_call_audits")
    op.drop_index("idx_rng_call_audits_action_id", table_name="rng_call_audits")
    op.drop_table("rng_call_audits")
    op.drop_index("idx_rng_action_audits_ledger_event_id", table_name="rng_action_audits")
    op.drop_index("idx_rng_action_audits_user_id", table_name="rng_action_audits")
    op.drop_table("rng_action_audits")
