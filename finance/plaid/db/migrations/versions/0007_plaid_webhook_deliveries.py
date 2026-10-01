"""Store complete authenticated Plaid webhook deliveries.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plaid_webhook_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_body", sa.Text(), nullable=False),
        sa.Column("webhook_type", sa.String(), nullable=True),
        sa.Column("webhook_code", sa.String(), nullable=True),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        comment="Audit of authenticated Plaid webhook deliveries, including the complete request body.",
    )
    op.create_index("idx_plaid_webhook_deliveries_received_at", "plaid_webhook_deliveries", ["received_at"])
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plaid_ro') THEN
                EXECUTE 'GRANT SELECT ON TABLE public.plaid_webhook_deliveries TO plaid_ro';
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("idx_plaid_webhook_deliveries_received_at", table_name="plaid_webhook_deliveries")
    op.drop_table("plaid_webhook_deliveries")
