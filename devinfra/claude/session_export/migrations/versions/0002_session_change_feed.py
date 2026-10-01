"""Durable replay journal for changes visible to session readers."""

import sqlalchemy as sa
from alembic import op

revision = "0002_session_change_feed"
down_revision = "0001_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_change_state",
        sa.Column("singleton", sa.SmallInteger(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("oldest_retained", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("singleton = 1", name="session_change_state_singleton"),
    )
    op.execute(sa.text("INSERT INTO session_change_state (singleton, revision, oldest_retained) VALUES (1, 0, 0)"))
    op.create_table(
        "session_changes",
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("session_id", sa.Text(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("session_changes")
    op.drop_table("session_change_state")
