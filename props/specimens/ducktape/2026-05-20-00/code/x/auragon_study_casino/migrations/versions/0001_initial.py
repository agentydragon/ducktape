"""Initial schema for the CNPG-backed study casino.

Revision ID: 0001
Revises:

Creates the full schema: every per-user table carries a `user_id` column
and queries scope by it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "balance",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("credits >= 0", name="balance_credits_nonneg"),
        sa.CheckConstraint("tokens >= 0", name="balance_tokens_nonneg"),
    )

    op.create_table(
        "sessions",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("subject", sa.String(length=120), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("ended_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("seconds >= 0", name="sessions_seconds_nonneg"),
        sa.CheckConstraint("length(subject) > 0", name="sessions_subject_nonempty"),
    )
    op.create_index("idx_sessions_user_ended_at", "sessions", ["user_id", "ended_at_ms"])

    op.create_table(
        "prizes",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.CheckConstraint("cost > 0", name="prizes_cost_positive"),
        sa.CheckConstraint("length(name) > 0", name="prizes_name_nonempty"),
    )

    op.create_table(
        "prize_log",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("cost", sa.Integer(), nullable=False),
        sa.Column("at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("cost >= 0", name="prize_log_cost_nonneg"),
    )
    op.create_index("idx_prize_log_user_at_ms", "prize_log", ["user_id", "at_ms"])

    op.create_table(
        "game_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("client_event_id", sa.String(length=128), nullable=False),
        sa.Column("server_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("game", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("wager_credits", sa.Integer(), nullable=False),
        sa.Column("payout_tokens", sa.Integer(), nullable=False),
        sa.Column("credits_before", sa.Integer(), nullable=False),
        sa.Column("credits_after", sa.Integer(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after", sa.Integer(), nullable=False),
        sa.Column("server_credits", sa.Integer(), nullable=False),
        sa.Column("server_tokens", sa.Integer(), nullable=False),
        sa.Column("outcome_json", sa.Text(), nullable=False),
        sa.Column("rules_version", sa.String(length=32), nullable=True),
        sa.Column("rng_version", sa.String(length=32), nullable=True),
        sa.UniqueConstraint("user_id", "client_event_id", name="game_events_user_client_event_id_unique"),
    )
    op.create_index("idx_game_events_user_id", "game_events", ["user_id", "id"])

    op.create_table(
        "ledger_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("client_action_id", sa.String(length=128), nullable=False),
        sa.Column("server_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("rules_version", sa.String(length=32), nullable=False),
        sa.Column("rng_version", sa.String(length=32), nullable=True),
        sa.Column("credits_before", sa.Integer(), nullable=False),
        sa.Column("credits_after", sa.Integer(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("user_id", "client_action_id", name="ledger_events_user_client_action_id_unique"),
    )
    op.create_index("idx_ledger_events_user_id", "ledger_events", ["user_id", "id"])

    op.create_table(
        "state_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("server_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("decoded_json", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("idx_state_snapshots_user_id", "state_snapshots", ["user_id", "id"])

    op.create_table(
        "blackjack_hands",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("wager_credits", sa.Integer(), nullable=False),
        sa.Column("current_wager_credits", sa.Integer(), nullable=False),
        sa.Column("credits_before", sa.Integer(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("shoe_json", sa.Text(), nullable=False),
        sa.Column("player_json", sa.Text(), nullable=False),
        sa.Column("dealer_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
    )
    op.create_index("idx_blackjack_hands_user_status", "blackjack_hands", ["user_id", "status"])

    # Drop server_default on balance now that the row will be inserted explicitly
    # by the application (lazy seed on first action for a new user).
    with op.batch_alter_table("balance") as batch:
        batch.alter_column("credits", server_default=None)
        batch.alter_column("tokens", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_blackjack_hands_user_status", table_name="blackjack_hands")
    op.drop_table("blackjack_hands")
    op.drop_index("idx_state_snapshots_user_id", table_name="state_snapshots")
    op.drop_table("state_snapshots")
    op.drop_index("idx_ledger_events_user_id", table_name="ledger_events")
    op.drop_table("ledger_events")
    op.drop_index("idx_game_events_user_id", table_name="game_events")
    op.drop_table("game_events")
    op.drop_index("idx_prize_log_user_at_ms", table_name="prize_log")
    op.drop_table("prize_log")
    op.drop_index("idx_prizes_user", table_name="prizes")
    op.drop_table("prizes")
    op.drop_index("idx_sessions_user_ended_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("balance")
