"""Record Plaid Link transaction history depth.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "links",
        sa.Column(
            "transaction_days_requested",
            sa.Integer(),
            nullable=True,
            comment="Transactions days_requested value used when Transactions was initialized through Plaid Link.",
        ),
    )


def downgrade() -> None:
    op.drop_column("links", "transaction_days_requested")
