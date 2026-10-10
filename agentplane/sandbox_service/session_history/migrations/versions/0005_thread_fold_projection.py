"""Tables for the History Service's copy of the thread fold, rebuilt from the raw log.

The History Service writes them; nothing in the Sandbox Service reads them.
"""

from alembic import op

revision = "0005_thread_fold_projection"
down_revision = "0004_session_change_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE session_projection_lease (
            session_id UUID NOT NULL,
            token UUID NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (session_id)
        )
    """)
    op.execute("""
        CREATE TABLE thread_checkpoint (
            thread_id UUID NOT NULL,
            source_id TEXT NOT NULL,
            projection_epoch TEXT NOT NULL,
            through_cursor BIGINT NOT NULL,
            PRIMARY KEY (thread_id)
        )
    """)
    op.execute("""
        CREATE TABLE thread_entity (
            thread_id UUID NOT NULL,
            projection_epoch TEXT NOT NULL,
            entity_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            cursor BIGINT NOT NULL,
            revision_cursor BIGINT NOT NULL,
            pending BOOLEAN NOT NULL,
            turn_id TEXT,
            state JSONB NOT NULL,
            text_ref JSONB,
            arguments_ref JSONB,
            output_ref JSONB,
            input_ref JSONB,
            entity_index BIGINT NOT NULL,
            PRIMARY KEY (thread_id, projection_epoch, entity_kind, entity_id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_thread_entity_scope_cursor ON thread_entity (thread_id, projection_epoch, cursor, entity_kind, entity_id)
    """)
    op.execute("""
        CREATE UNIQUE INDEX ix_thread_entity_scope_entity_index ON thread_entity (thread_id, projection_epoch, entity_index)
    """)
    op.execute("""
        CREATE INDEX ix_thread_entity_scope_pending_cursor ON thread_entity (thread_id, projection_epoch, cursor, entity_kind, entity_id) WHERE pending
    """)
    op.execute("""
        CREATE INDEX ix_thread_entity_scope_revision ON thread_entity (thread_id, projection_epoch, revision_cursor)
    """)
    op.execute("""
        CREATE INDEX ix_thread_entity_scope_segment_cursor ON thread_entity (thread_id, projection_epoch, cursor) WHERE entity_kind IN ('item', 'confirmed_input', 'lifecycle')
    """)
    op.execute("""
        CREATE TABLE thread_evidence (
            thread_id UUID NOT NULL,
            projection_epoch TEXT NOT NULL,
            entity_cursor BIGINT NOT NULL,
            observation_cursor BIGINT NOT NULL,
            PRIMARY KEY (thread_id, projection_epoch, entity_cursor, observation_cursor)
        )
    """)
    op.execute("""
        CREATE TABLE thread_history_summary (
            thread_id UUID NOT NULL,
            last_event_at TIMESTAMP WITH TIME ZONE,
            last_turn_status BIGINT,
            attached JSONB,
            "end" JSONB,
            resumed_after_cursor BIGINT,
            last_model_activity_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (thread_id)
        )
    """)
    op.execute("""
        CREATE TABLE thread_native_link (
            thread_id UUID NOT NULL,
            projection_epoch TEXT NOT NULL,
            entity_cursor BIGINT NOT NULL,
            observation_cursor BIGINT NOT NULL,
            source_sequence BIGINT NOT NULL,
            PRIMARY KEY (thread_id, projection_epoch, entity_cursor, observation_cursor, source_sequence)
        )
    """)
    op.execute("""
        CREATE TABLE thread_payload_chunk (
            thread_id UUID NOT NULL,
            projection_epoch TEXT NOT NULL,
            owner_cursor BIGINT NOT NULL,
            owner_id TEXT NOT NULL,
            field TEXT NOT NULL,
            generation BIGINT NOT NULL,
            chunk_index BIGINT NOT NULL,
            text JSON NOT NULL,
            PRIMARY KEY (thread_id, projection_epoch, owner_cursor, owner_id, field, generation, chunk_index)
        )
    """)
    op.execute("""
        CREATE TABLE thread_payload_manifest (
            thread_id UUID NOT NULL,
            projection_epoch TEXT NOT NULL,
            owner_cursor BIGINT NOT NULL,
            owner_id TEXT NOT NULL,
            field TEXT NOT NULL,
            generation BIGINT NOT NULL,
            revision_cursor BIGINT NOT NULL,
            chunk_count BIGINT NOT NULL,
            content_bytes BIGINT NOT NULL,
            PRIMARY KEY (thread_id, projection_epoch, owner_cursor, owner_id, field, generation, revision_cursor)
        )
    """)


def downgrade() -> None:
    for table in (
        "thread_native_link",
        "thread_evidence",
        "thread_payload_chunk",
        "thread_payload_manifest",
        "thread_entity",
        "thread_history_summary",
        "thread_checkpoint",
        "session_projection_lease",
    ):
        op.drop_table(table)
