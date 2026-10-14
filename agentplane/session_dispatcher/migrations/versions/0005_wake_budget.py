"""Persist authenticated caller wake budgets across retries and replicas."""

import sqlalchemy as sa
from alembic import op

revision = "0005_wake_budget"
down_revision = "0004_sandbox_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dispatch", "session_opening"):
        op.add_column(table, sa.Column("wake_caller_namespace", sa.String(), nullable=True))
        op.add_column(table, sa.Column("wake_caller_name", sa.String(), nullable=True))

    op.create_table(
        "wake_attempt",
        sa.Column("caller_namespace", sa.String(), primary_key=True, nullable=False),
        sa.Column("caller_name", sa.String(), primary_key=True, nullable=False),
        sa.Column("sandbox_uid", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("wake_generation", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wake_attempt_caller_time", "wake_attempt", ["caller_namespace", "caller_name", "attempted_at"])
