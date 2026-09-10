"""Drop the chat Recall index: chat_chunks, chat_chunk_messages, chat_sessions.

The console no longer hosts chat sessions to index (0129 dropped their source tables), and the
chat-corpus reader/writer and ``IndexType.CHAT`` are gone from ``haku/recall_index`` alongside them.
Their ``recall_index`` schema occurrences and the ``haku-conversations`` logical index registration
are the last physical remnants.

Revision ID: 0130
Revises: 0129
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0130"
down_revision: str | None = "0129"
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA = "recall_index"
_CONVERSATIONS = "haku-conversations"


def _table(name: str) -> str:
    return f"{SCHEMA}.{name}"


def upgrade() -> None:
    op.drop_table("chat_chunk_messages", schema=SCHEMA)
    op.drop_table("chat_chunks", schema=SCHEMA)
    op.drop_table("chat_sessions", schema=SCHEMA)
    op.execute(f"DELETE FROM {_table('indexes')} WHERE index_id = '{_CONVERSATIONS}'")
    op.drop_constraint("ck_indexes_index_type", "indexes", schema=SCHEMA, type_="check")
    op.create_check_constraint("ck_indexes_index_type", "indexes", "index_type IN ('git')", schema=SCHEMA)


def downgrade() -> None:
    op.drop_constraint("ck_indexes_index_type", "indexes", schema=SCHEMA, type_="check")
    op.create_check_constraint("ck_indexes_index_type", "indexes", "index_type IN ('git', 'chat')", schema=SCHEMA)
    op.execute(f"INSERT INTO {_table('indexes')} (index_id, index_type) VALUES ('{_CONVERSATIONS}', 'chat')")

    op.create_table(
        "chat_sessions",
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chunker_key", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("index_id", "session_id", name="chat_sessions_pkey"),
        sa.ForeignKeyConstraint(["index_id"], [f"{SCHEMA}.indexes.index_id"], name="chat_sessions_index_id_fkey"),
        schema=SCHEMA,
    )
    op.create_table(
        "chat_chunks",
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("window_no", sa.Integer(), nullable=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("index_id", "session_id", "window_no", name="chat_chunks_pkey"),
        sa.ForeignKeyConstraint(
            ["content_sha"], [f"{SCHEMA}.contents.content_sha"], name="chat_chunks_content_sha_fkey"
        ),
        sa.ForeignKeyConstraint(["index_id"], [f"{SCHEMA}.indexes.index_id"], name="chat_chunks_index_id_fkey"),
        schema=SCHEMA,
    )
    op.create_table(
        "chat_chunk_messages",
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("window_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("message_id", UUID(as_uuid=True), nullable=False),
        sa.Column("index_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("index_id", "session_id", "window_no", "ordinal", name="chat_chunk_messages_pkey"),
        sa.ForeignKeyConstraint(
            ["index_id", "session_id", "window_no"],
            [f"{SCHEMA}.chat_chunks.index_id", f"{SCHEMA}.chat_chunks.session_id", f"{SCHEMA}.chat_chunks.window_no"],
            name="chat_chunk_messages_index_id_session_id_window_no_fkey",
            ondelete="CASCADE",
        ),
        schema=SCHEMA,
    )
    op.create_index("idx_chat_chunk_messages_message", "chat_chunk_messages", ["message_id"], schema=SCHEMA)
