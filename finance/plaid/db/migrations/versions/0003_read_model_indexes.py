"""Add indexes for Plaid read-model queries.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("idx_accounts_item_id", "accounts", ["item_id"])
    op.create_index(
        "idx_transactions_budget_account_date_id",
        "transactions",
        ["account_id", "date", "transaction_id"],
        postgresql_where=sa.text("removed = false AND pending = false"),
    )
    op.create_index(
        "idx_transactions_active_item_date",
        "transactions",
        ["item_id", "date"],
        postgresql_where=sa.text("removed = false"),
    )
    op.create_index(
        "idx_investment_transactions_account_date",
        "investment_transactions",
        ["account_id", "date", "investment_transaction_id"],
        postgresql_where=sa.text("removed = false"),
    )
    op.execute(
        """
        CREATE INDEX idx_balance_snapshots_account_latest
        ON balance_snapshots (account_id, captured_at DESC, id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_holding_snapshots_account_security_latest
        ON holding_snapshots (account_id, security_id, captured_at DESC, id DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_holding_snapshots_account_security_latest")
    op.execute("DROP INDEX IF EXISTS idx_balance_snapshots_account_latest")
    op.drop_index("idx_investment_transactions_account_date", table_name="investment_transactions")
    op.drop_index("idx_transactions_active_item_date", table_name="transactions")
    op.drop_index("idx_transactions_budget_account_date_id", table_name="transactions")
    op.drop_index("idx_accounts_item_id", table_name="accounts")
