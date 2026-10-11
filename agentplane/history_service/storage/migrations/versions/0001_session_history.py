"""Create Sandbox Service-owned Session Event history."""

import sqlalchemy as sa
from alembic import op

revision = "0001_session_history"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_history",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("sandbox_namespace", sa.String(), nullable=False),
        sa.Column("sandbox_name", sa.String(), nullable=False),
        sa.Column("sandbox_uid", sa.Uuid(), nullable=True),
        sa.Column("runner_session_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("last_cursor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("last_cursor >= 0", name="history_last_cursor_nonnegative"),
    )
    op.create_table(
        "session_event",
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("session_history.id"), primary_key=True, nullable=False),
        sa.Column("cursor", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_event")
    op.drop_table("session_history")
