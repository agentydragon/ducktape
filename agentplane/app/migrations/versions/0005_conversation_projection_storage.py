"""Create materialized conversation storage; deployed staging databases must be reset."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_conversation_projection"
down_revision = "0004_followable_event_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_event_thread_at", "event", ["thread_id", "at"])
    op.add_column("event", sa.Column("origin_source_id", sa.Text(), nullable=False))
    op.add_column("event", sa.Column("origin_sequence", sa.BigInteger(), nullable=False))
    op.create_index("ix_event_thread_origin", "event", ["thread_id", "origin_source_id", "origin_sequence"])
    op.create_table(
        "conversation_projection_checkpoint",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("projection_epoch", sa.Text(), nullable=False),
        sa.Column("through_cursor", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "conversation_entity",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("projection_epoch", sa.Text(), primary_key=True),
        sa.Column("entity_kind", sa.Text(), primary_key=True),
        sa.Column("entity_id", sa.Text(), primary_key=True),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.Column("revision_cursor", sa.BigInteger(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("turn_id", sa.Text()),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("text_ref", postgresql.JSONB(none_as_null=True)),
        sa.Column("arguments_ref", postgresql.JSONB(none_as_null=True)),
        sa.Column("output_ref", postgresql.JSONB(none_as_null=True)),
        sa.Column("input_ref", postgresql.JSONB(none_as_null=True)),
    )
    op.create_index(
        "ix_conversation_entity_scope_revision",
        "conversation_entity",
        ["thread_id", "source_id", "projection_epoch", "revision_cursor"],
    )
    op.create_index(
        "ix_conversation_entity_scope_cursor",
        "conversation_entity",
        ["thread_id", "source_id", "projection_epoch", "cursor", "entity_kind", "entity_id"],
        postgresql_where=sa.text("entity_kind IN ('item', 'confirmed_input', 'lifecycle')"),
    )
    op.create_index(
        "ix_conversation_entity_scope_pending_cursor",
        "conversation_entity",
        ["thread_id", "source_id", "projection_epoch", "cursor", "entity_kind", "entity_id"],
        postgresql_where=sa.text("pending"),
    )
    op.create_index(
        "ix_conversation_entity_scope_segment_cursor",
        "conversation_entity",
        ["thread_id", "source_id", "projection_epoch", "cursor"],
        postgresql_where=sa.text("entity_kind IN ('item', 'confirmed_input', 'lifecycle')"),
    )
    op.create_table(
        "conversation_payload_manifest",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("projection_epoch", sa.Text(), primary_key=True),
        sa.Column("owner_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("owner_id", sa.Text(), primary_key=True),
        sa.Column("field", sa.Text(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), primary_key=True),
        sa.Column("revision_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("present", sa.Boolean(), nullable=False),
        sa.Column("chunk_count", sa.BigInteger(), nullable=False),
        sa.Column("content_bytes", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "conversation_payload_chunk",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("projection_epoch", sa.Text(), primary_key=True),
        sa.Column("owner_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("owner_id", sa.Text(), primary_key=True),
        sa.Column("field", sa.Text(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), primary_key=True),
        sa.Column("chunk_index", sa.BigInteger(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_table(
        "conversation_projection_evidence",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("projection_epoch", sa.Text(), primary_key=True),
        sa.Column("entity_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("observation_cursor", sa.BigInteger(), primary_key=True),
    )
    op.create_table(
        "conversation_projection_native_link",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("projection_epoch", sa.Text(), primary_key=True),
        sa.Column("entity_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("observation_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("source_sequence", sa.BigInteger(), primary_key=True),
    )
    synced_tables = (
        "conversation_projection_checkpoint",
        "conversation_entity",
        "conversation_payload_manifest",
        "conversation_payload_chunk",
    )
    for table in synced_tables:
        op.execute(f'ALTER TABLE "{table}" REPLICA IDENTITY FULL')
    op.execute("GRANT USAGE ON SCHEMA public TO electric")
    op.execute(f"GRANT SELECT ON {', '.join(synced_tables)} TO electric")
    op.execute(f"CREATE PUBLICATION electric_publication_agentplane_conversation FOR TABLE {', '.join(synced_tables)}")


def downgrade() -> None:
    op.execute("DROP PUBLICATION IF EXISTS electric_publication_agentplane_conversation")
    op.drop_table("conversation_projection_evidence")
    op.drop_table("conversation_projection_native_link")
    op.drop_table("conversation_payload_chunk")
    op.drop_table("conversation_payload_manifest")
    op.drop_index("ix_conversation_entity_scope_segment_cursor", table_name="conversation_entity")
    op.drop_index("ix_conversation_entity_scope_pending_cursor", table_name="conversation_entity")
    op.drop_index("ix_conversation_entity_scope_cursor", table_name="conversation_entity")
    op.drop_index("ix_conversation_entity_scope_revision", table_name="conversation_entity")
    op.drop_table("conversation_entity")
    op.drop_table("conversation_projection_checkpoint")
    op.drop_index("ix_event_thread_at", table_name="event")
    op.drop_index("ix_event_thread_origin", table_name="event")
    op.drop_column("event", "origin_sequence")
    op.drop_column("event", "origin_source_id")
