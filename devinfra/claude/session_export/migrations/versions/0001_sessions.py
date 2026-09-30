"""Claude Code sessions and their events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_sessions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False, comment="As the API lists it now."),
        sa.Column(
            "synced_last_event_at",
            sa.DateTime(timezone=True),
            comment="`last_event_at` as listed before the last completed event pass; NULL until the first one finishes.",
        ),
        sa.Column("raw", postgresql.JSONB(), nullable=False, comment="The list item as sent."),
    )
    op.create_table(
        "events",
        sa.Column("session_id", sa.Text(), sa.ForeignKey("sessions.session_id"), primary_key=True),
        sa.Column("sequence_num", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("event_id", sa.Uuid(), nullable=False, comment="Not unique: the API repeats ids across sessions."),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Not monotonic in `sequence_num`; order by that.",
        ),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("processing_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("events_by_type", "events", ["event_type", "created_at"])
    # Same size as the default pglz, faster to write.
    op.execute("ALTER TABLE events ALTER COLUMN payload SET COMPRESSION lz4")


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("sessions")
