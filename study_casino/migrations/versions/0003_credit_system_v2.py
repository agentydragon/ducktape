"""Credit system v2: millicredit units, token-economy rebalance, streak state.

Three coordinated changes:

1. Credit columns reinterpreted as integer millicredits (credit value
   × 1000). Existing rows are whole credits, so ×1000 converts in place.
2. Token-economy rebalance: the v2 boosts (streak multiplier up to 2x,
   +30/day bonus, fractional earning) roughly double steady-state credit
   income, so prize costs double to keep prize difficulty constant in
   study-time terms — and existing token balances double so already-earned
   tokens keep their prize purchasing power. Append-only audit records
   (`prize_log`, `game_events` token snapshots, `ledger_events` token
   snapshots) keep their historical values; this migration is the
   documented discontinuity.
3. New per-user `credit_state` table for streak / daily-bonus state.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Steady-state credit-income inflation from the v2 boosts (multiplier cap is
# 2x; the daily bonus approximately covers the sub-cap ramp).
_PRIZE_REBALANCE_FACTOR = 2


def upgrade() -> None:
    op.execute("UPDATE balance SET credits = credits * 1000")
    op.execute("UPDATE ledger_events SET credits_before = credits_before * 1000, credits_after = credits_after * 1000")
    op.execute(
        "UPDATE game_events SET credits_before = credits_before * 1000, credits_after = credits_after * 1000, "
        "server_credits = server_credits * 1000"
    )
    op.execute("UPDATE blackjack_hands SET credits_before = credits_before * 1000")

    op.execute(f"UPDATE balance SET tokens = tokens * {_PRIZE_REBALANCE_FACTOR}")
    op.execute(f"UPDATE prizes SET cost = cost * {_PRIZE_REBALANCE_FACTOR}")

    op.create_table(
        "credit_state",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("streak_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_qualifying_date", sa.String(length=10), nullable=True),
        sa.Column("rest_days_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_first_bonus_date", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("credit_state")

    op.execute(f"UPDATE prizes SET cost = cost / {_PRIZE_REBALANCE_FACTOR}")
    op.execute(f"UPDATE balance SET tokens = tokens / {_PRIZE_REBALANCE_FACTOR}")

    op.execute("UPDATE blackjack_hands SET credits_before = credits_before / 1000")
    op.execute(
        "UPDATE game_events SET credits_before = credits_before / 1000, credits_after = credits_after / 1000, "
        "server_credits = server_credits / 1000"
    )
    op.execute("UPDATE ledger_events SET credits_before = credits_before / 1000, credits_after = credits_after / 1000")
    op.execute("UPDATE balance SET credits = credits / 1000")
