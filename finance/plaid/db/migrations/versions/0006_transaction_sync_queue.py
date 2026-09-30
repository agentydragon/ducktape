"""Add a durable transaction-sync queue and remove obsolete status state.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("links", "transactions_update_status")
    op.alter_column(
        "links",
        "transactions_cursor",
        existing_type=sa.String(),
        comment="Persisted cursor for the Item's /transactions/sync delta stream.",
    )
    op.alter_column(
        "transactions",
        "removed",
        existing_type=sa.Boolean(),
        comment="True when Plaid included this transaction in a /transactions/sync removed delta; retain as a tombstone.",
    )
    op.execute(
        "COMMENT ON TABLE transactions IS 'Plaid Transaction objects maintained from cursor-based /transactions/sync deltas.'"
    )
    op.alter_column(
        "sync_runs",
        "mode",
        existing_type=sa.String(),
        comment="Sync algorithm name; current mode is v1_cursor_transactions.",
    )
    op.alter_column(
        "sync_runs",
        "configured_windows",
        existing_type=JSONB(),
        comment="JSON object recording cursor-based Transactions sync and the investment transaction day window.",
    )
    op.execute(
        "COMMENT ON TABLE sync_runs IS 'Append-only sync run ledger correlating per-Item syncs with Plaid API audit events.'"
    )
    op.create_table(
        "transaction_sync_queue",
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        comment="Coalesced durable requests to sync transaction deltas for Plaid Items.",
    )
    op.create_index("idx_transaction_sync_queue_requested", "transaction_sync_queue", ["requested_at"])


def downgrade() -> None:
    op.drop_index("idx_transaction_sync_queue_requested", table_name="transaction_sync_queue")
    op.drop_table("transaction_sync_queue")
    op.add_column("links", sa.Column("transactions_update_status", sa.String(), nullable=True))
    op.alter_column(
        "links",
        "transactions_cursor",
        existing_type=sa.String(),
        comment="Reserved for v1 /transactions/sync cursor state; v0 full refresh leaves it null.",
    )
    op.alter_column(
        "transactions",
        "removed",
        existing_type=sa.Boolean(),
        comment="True when Plaid no longer returns the transaction in the refreshed window or v1 removed set; do not hard-delete.",
    )
    op.execute(
        "COMMENT ON TABLE transactions IS 'Plaid Transaction objects reconciled by v0 full-refresh windows or v1 /transactions/sync updates.'"
    )
    op.alter_column(
        "sync_runs", "mode", existing_type=sa.String(), comment="Sync algorithm name, currently v0_full_refresh."
    )
    op.alter_column(
        "sync_runs",
        "configured_windows",
        existing_type=JSONB(),
        comment="JSON object recording transaction and investment transaction day windows used by this run.",
    )
    op.execute(
        "COMMENT ON TABLE sync_runs IS 'Append-only sync run ledger used to correlate full-refresh windows and Plaid API audit events.'"
    )
