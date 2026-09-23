"""Give the copied Event sequence its own identity below the thread.

`thread` held both what a runner session is (sandbox, session, harness, model, cwd) and what an
operator set on it (name, archived). The first moves to `event_log`, which ingestion mints and
everything recorded hangs off; `thread` keeps only the second, and only once something is set.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_event_log_identity"
down_revision = "0010_drop_unread_fold_state"
branch_labels = None
depends_on = None

# Every table whose `thread_id` keys what was recorded for an event log.
_RECORDED = (
    "event",
    "feed_state",
    "thread_checkpoint",
    "thread_entity",
    "thread_payload_manifest",
    "thread_payload_chunk",
    "thread_evidence",
    "thread_native_link",
)
_IDENTITY = ("sandbox", "session_id", "harness", "model", "cwd", "created_at")


def _drop_foreign_keys_to(table: str) -> None:
    # Created unnamed, and 0006 renamed several of the tables that carry them, so each constraint is
    # found by what it references rather than by a name it may not have.
    op.execute(
        f"""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT conrelid::regclass AS referrer, conname FROM pg_constraint
                     WHERE contype = 'f' AND confrelid = '{table}'::regclass
            LOOP
                EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.referrer, r.conname);
            END LOOP;
        END $$
        """
    )


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sandbox", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("harness", sa.String(length=14), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sandbox", "session_id"),
    )
    columns = ", ".join(("id", *_IDENTITY))
    op.execute(f"INSERT INTO event_log ({columns}) SELECT {columns} FROM thread")
    _drop_foreign_keys_to("thread")
    for table in _RECORDED:
        op.create_foreign_key(None, table, "event_log", ["thread_id"], ["id"], ondelete="CASCADE")
    # Dropping a column drops the (sandbox, session_id) unique constraint it belongs to.
    for column in _IDENTITY:
        op.drop_column("thread", column)
    op.create_foreign_key(None, "thread", "event_log", ["id"], ["id"], ondelete="CASCADE")
    op.execute("DELETE FROM thread WHERE name IS NULL AND NOT archived")


def downgrade() -> None:
    for column in _IDENTITY:
        op.add_column("thread", sa.Column(column, sa.Text() if column != "created_at" else sa.DateTime(timezone=True)))
    op.execute("INSERT INTO thread (id, archived) SELECT id, false FROM event_log ON CONFLICT (id) DO NOTHING")
    assignments = ", ".join(f"{column} = log.{column}" for column in _IDENTITY)
    op.execute(f"UPDATE thread SET {assignments} FROM event_log AS log WHERE log.id = thread.id")
    for column in _IDENTITY:
        op.alter_column("thread", column, nullable=False)
    op.alter_column("thread", "harness", type_=sa.String(length=14), existing_type=sa.Text(), existing_nullable=False)
    _drop_foreign_keys_to("event_log")
    for table in _RECORDED:
        op.create_foreign_key(None, table, "thread", ["thread_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint(None, "thread", ["sandbox", "session_id"])
    op.drop_table("event_log")
