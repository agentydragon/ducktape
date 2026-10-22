"""Track complete account observations and detached Plaid accounts.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "links",
        sa.Column(
            "accounts_observed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last successful complete /accounts/get listing for this Item.",
        ),
    )
    op.add_column(
        "accounts",
        sa.Column(
            "detached_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="First successful complete /accounts/get listing for this Item that omitted this account.",
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "detached_at")
    op.drop_column("links", "accounts_observed_at")
