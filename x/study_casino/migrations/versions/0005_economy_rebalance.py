"""Recalibrate the token economy from 2x to 1.2x legacy prices.

Credit system v2 migration 0003 doubled prize costs and existing token
balances. Replaying actual study behavior showed a roughly 1.2x earnings
increase instead, so scale both sides of the token economy by 3/5. Scaling
balances with prices preserves each user's existing purchasing progress.

Values are non-negative integers. Division rounds to the nearest token; the
downgrade is therefore approximate by at most one token.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Round 3/5 to the nearest integer. Cast through bigint so the
    # multiplication cannot overflow an Integer column before division.
    op.execute("UPDATE balance SET tokens = ((tokens::bigint * 3 + 2) / 5)::integer")
    op.execute("UPDATE prizes SET cost = ((cost::bigint * 3 + 2) / 5)::integer")


def downgrade() -> None:
    # Nearest-integer inverse of 3/5. Integer rounding in upgrade means exact
    # original values cannot always be reconstructed.
    op.execute("UPDATE prizes SET cost = ((cost::bigint * 5 + 1) / 3)::integer")
    op.execute("UPDATE balance SET tokens = ((tokens::bigint * 5 + 1) / 3)::integer")
