"""Fold `event_log` into `thread`: every Thread the app opened gets a `thread` row with its creation time.

`event_log` held only a Thread's ID and creation time, and `thread` what an operator set on it, in a
row created only once something was set.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035_event_log_into_thread"
down_revision = "0034_thread_resume_drop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("INSERT INTO thread (id, archived) SELECT id, false FROM event_log ON CONFLICT (id) DO NOTHING")
    op.execute("UPDATE thread SET created_at = log.created_at FROM event_log AS log WHERE log.id = thread.id")
    op.alter_column("thread", "created_at", nullable=False)
    op.drop_constraint("thread_id_fkey", "thread", type_="foreignkey")
    op.drop_table("event_log")


def downgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO event_log (id, created_at) SELECT id, created_at FROM thread")
    op.create_foreign_key("thread_id_fkey", "thread", "event_log", ["id"], ["id"], ondelete="CASCADE")
    op.drop_column("thread", "created_at")
    op.execute("DELETE FROM thread WHERE name IS NULL AND NOT archived")
