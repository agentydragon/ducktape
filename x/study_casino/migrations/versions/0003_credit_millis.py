"""Reinterpret credit columns as integer millicredits (credit value × 1000).

Existing rows are in whole credits, so ×1000 converts them in place.
Tokens and wager columns stay whole units.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE balance SET credits = credits * 1000")
    op.execute("UPDATE ledger_events SET credits_before = credits_before * 1000, credits_after = credits_after * 1000")
    op.execute(
        "UPDATE game_events SET credits_before = credits_before * 1000, credits_after = credits_after * 1000, "
        "server_credits = server_credits * 1000"
    )
    op.execute("UPDATE blackjack_hands SET credits_before = credits_before * 1000")


def downgrade() -> None:
    op.execute("UPDATE blackjack_hands SET credits_before = credits_before / 1000")
    op.execute(
        "UPDATE game_events SET credits_before = credits_before / 1000, credits_after = credits_after / 1000, "
        "server_credits = server_credits / 1000"
    )
    op.execute("UPDATE ledger_events SET credits_before = credits_before / 1000, credits_after = credits_after / 1000")
    op.execute("UPDATE balance SET credits = credits / 1000")
