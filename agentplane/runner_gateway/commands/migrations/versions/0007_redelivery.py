"""Ambiguous submissions settle by redelivery; nothing reads runner journals for admissions."""

import sqlalchemy as sa
from alembic import op

revision = "0007_redelivery"
down_revision = "0006_connection_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_command_submission_pending", table_name="command_submission")
    op.drop_table("admission_reconciliation")


def downgrade() -> None:
    # Cursors restart at zero; reconciliation replays a journal idempotently.
    op.create_table(
        "admission_reconciliation",
        sa.Column("session_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("reconciled_through", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("reconciled_through >= 0", name="reconciled_through_nonnegative"),
    )
    op.create_index(
        "ix_command_submission_pending",
        "command_submission",
        ["session_id"],
        postgresql_where=sa.text("state = 'pending_admission'"),
    )
