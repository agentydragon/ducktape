"""Create the integration app's canonical state: threads, their events, and operator sessions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_trajectory_store"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sandbox", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text()),
        sa.UniqueConstraint("sandbox", "session_id"),
    )
    op.create_table(
        "event",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "sandbox_ingestion",
        sa.Column("sandbox", sa.Text(), primary_key=True),
        sa.Column("token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "feed_state",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("attached", postgresql.JSONB(), nullable=False),
        sa.Column("end", postgresql.JSONB(none_as_null=True)),
    )
    op.create_table(
        "operator_browser_session",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_operator_browser_session_expires_at", "operator_browser_session", ["expires_at"])


def downgrade() -> None:
    op.drop_table("operator_browser_session")
    op.drop_table("feed_state")
    op.drop_table("sandbox_ingestion")
    op.drop_table("event")
    op.drop_table("thread")
