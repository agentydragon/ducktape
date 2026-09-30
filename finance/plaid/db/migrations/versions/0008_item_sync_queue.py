"""Generalize the webhook queue to support full Item refreshes.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("idx_transaction_sync_queue_requested", table_name="transaction_sync_queue")
    op.drop_table("transaction_sync_queue")
    op.create_table(
        "item_sync_queue",
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("full_sync", sa.Boolean(), nullable=False),
        comment="Coalesced durable per-Item webhook work; full_sync selects the full product refresh path.",
    )
    op.create_index("idx_item_sync_queue_requested", "item_sync_queue", ["requested_at"])


def downgrade() -> None:
    op.drop_index("idx_item_sync_queue_requested", table_name="item_sync_queue")
    op.drop_table("item_sync_queue")
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
