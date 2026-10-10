"""Drop the fold tables' foreign keys to `event_log`.

The fold's models are shared with the History Service, whose database has no `event_log`. Nothing
deletes `event_log` rows, so the cascade these keys declared never ran.
"""

from alembic import op

revision = "0026_fold_keys_drop"
down_revision = "0025_summary_model_activity"
branch_labels = None
depends_on = None

FOLD_TABLES = (
    "session_projection_lease",
    "thread_checkpoint",
    "thread_history_summary",
    "thread_entity",
    "thread_payload_manifest",
    "thread_payload_chunk",
    "thread_evidence",
    "thread_native_link",
)
_KEY_COLUMN = {"session_projection_lease": "session_id"}


def upgrade() -> None:
    tables = ", ".join(f"'{table}'" for table in FOLD_TABLES)
    op.execute(f"""
        DO $$
        DECLARE key record;
        BEGIN
            FOR key IN
                SELECT conrelid::regclass AS owner, conname
                FROM pg_constraint
                WHERE contype = 'f' AND confrelid = 'event_log'::regclass
                    AND conrelid::regclass::text IN ({tables})
            LOOP
                EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', key.owner, key.conname);
            END LOOP;
        END $$
    """)


def downgrade() -> None:
    for table in FOLD_TABLES:
        column = _KEY_COLUMN.get(table, "thread_id")
        op.create_foreign_key(None, table, "event_log", [column], ["id"], ondelete="CASCADE")
