"""Index bounded unresolved-command pages; staging databases remain disposable."""

import sqlalchemy as sa
from alembic import op

revision = "0006_pending_interest"
down_revision = "0005_conversation_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_conversation_entity_scope_pending_cursor", table_name="conversation_entity")
    op.create_index(
        "ix_conversation_entity_pending_command_cursor",
        "conversation_entity",
        ["thread_id", "source_id", "projection_epoch", "cursor", "entity_id"],
        postgresql_where=sa.text("pending AND entity_kind = 'command'"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_entity_pending_command_cursor", table_name="conversation_entity")
    op.create_index(
        "ix_conversation_entity_scope_pending_cursor",
        "conversation_entity",
        ["thread_id", "source_id", "projection_epoch", "cursor", "entity_kind", "entity_id"],
        postgresql_where=sa.text("pending"),
    )
