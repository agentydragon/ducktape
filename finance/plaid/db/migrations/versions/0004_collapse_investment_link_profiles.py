"""Collapse investments_holdings/investments_full into a single investments profile.

The two requested the same Plaid product and differed only in whether sync called
/investments/transactions/get. Sync no longer consults the profile for that, so they became the
same thing under two names.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE links SET link_profile = 'investments' "
        "WHERE link_profile IN ('investments_holdings', 'investments_full')"
    )


def downgrade() -> None:
    # 'investments_holdings' was the narrower of the two; the distinction it encoded is gone, so
    # this direction cannot recover which row was which.
    op.execute("UPDATE links SET link_profile = 'investments_full' WHERE link_profile = 'investments'")
