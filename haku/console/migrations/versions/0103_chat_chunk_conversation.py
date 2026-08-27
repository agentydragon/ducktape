"""Link chat Recall occurrences to their conversation.

Search authorization joins each chat window to the conversation's pinned ``access_profile_id``
(#4431 stage 5), so the occurrence must name its conversation. Existing chat occurrences are
derived rows the next recall sweep re-materializes from the console's own tables — embeddings are
content-addressed and survive untouched in ``contents``/``content_embeddings`` — so this deletes
them and adds the column NOT NULL instead of backfilling (the conversation-data allowance,
`haku/console/AGENTS.md`). Deliberately no foreign key into ``public.conversation``: the recall
schema references console rows by value, exactly as ``session_id`` already does.

Revision ID: 0103
Revises: 0102
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0103"
down_revision: str | None = "0102"
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA = "recall_index"


def upgrade() -> None:
    # chat_chunk_messages cascades from chat_chunks; chat_sessions state is deleted too so the
    # next sweep sees every session as unindexed and re-materializes it with the conversation link.
    op.execute(f"DELETE FROM {SCHEMA}.chat_chunks")
    op.execute(f"DELETE FROM {SCHEMA}.chat_sessions")
    op.add_column("chat_chunks", sa.Column("conversation_id", UUID(as_uuid=True), nullable=False), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("chat_chunks", "conversation_id", schema=SCHEMA)
