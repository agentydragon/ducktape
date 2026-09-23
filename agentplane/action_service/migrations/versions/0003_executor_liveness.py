"""Add executor-level heartbeats and per-Execution lease/reconciliation columns."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_executor_liveness"
down_revision = "0002_decision_provider_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_executor_heartbeat",
        sa.Column("executor_id", sa.Text(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("action_execution", sa.Column("executor_id", sa.Text()))
    op.add_column("action_execution", sa.Column("lease_token", postgresql.UUID(as_uuid=True)))
    op.add_column("action_execution", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("action_execution", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("action_execution", sa.Column("reconciled_at", sa.DateTime(timezone=True)))
    op.add_column("action_execution", sa.Column("reconciliation_source", sa.Text()))
    op.add_column("action_execution", sa.Column("reconciled_by", sa.Text()))
    op.create_index(
        "ix_action_execution_lease_sweep",
        "action_execution",
        ["lease_expires_at"],
        postgresql_where=sa.text("state IN ('dispatching', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_action_execution_lease_sweep", table_name="action_execution")
    op.drop_column("action_execution", "reconciled_by")
    op.drop_column("action_execution", "reconciliation_source")
    op.drop_column("action_execution", "reconciled_at")
    op.drop_column("action_execution", "heartbeat_at")
    op.drop_column("action_execution", "lease_expires_at")
    op.drop_column("action_execution", "lease_token")
    op.drop_column("action_execution", "executor_id")
    op.drop_table("action_executor_heartbeat")
