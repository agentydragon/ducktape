"""Normalize the derived semantic index around globally-addressed content.

The old ``state_index.chunks`` table joined two incompatible identities: Git blob addresses and the
hash of rendered chat windows, so it scoped vectors to a corpus and a chunker regime even when the
exact input string had already been embedded elsewhere.

The replacement has three layers:

- ``contents``: ``content_sha -> content`` for the exact normalized UTF-8 input;
- ``content_embeddings``: one vector for that input under one model key; and
- Git/chat occurrence tables that retain source-specific provenance and refer to content.

The index is dropped rather than converted in place: a global content identity cannot be
reconstructed from the old Git blob-oriented rows without re-reading every source. The first regular
sync rebuilds it under the new model.

Deliberately incompatible for the duration of a rolling deploy, as 0038 was: an old replica may
issue an index query while the schema is being replaced, but the index is an optional, self-healing
derived projection and has no writers outside this service.

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from haku.recall_index.vector_type import HalfVector

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA = "state_index"


def upgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    op.execute(f"CREATE SCHEMA {SCHEMA}")

    op.create_table(
        "contents",
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("content_sha"),
        schema=SCHEMA,
    )
    op.create_table(
        "content_embeddings",
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("embedding", HalfVector(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["content_sha"], [f"{SCHEMA}.contents.content_sha"]),
        sa.PrimaryKeyConstraint("content_sha", "model_key"),
        schema=SCHEMA,
    )
    op.create_table(
        "git_chunks",
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.Column("chunker_key", sa.Text(), nullable=False),
        sa.Column("byte_start", sa.BigInteger(), nullable=False),
        sa.Column("byte_end", sa.BigInteger(), nullable=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["content_sha"], [f"{SCHEMA}.contents.content_sha"]),
        sa.PrimaryKeyConstraint("blob_sha", "chunker_key", "byte_start"),
        schema=SCHEMA,
    )
    op.create_table(
        "git_tip",
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("path"),
        schema=SCHEMA,
    )
    op.create_table(
        "git_sync_state",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("remote_commit", sa.Text(), nullable=True),
        sa.Column("remote_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("chunker_key", sa.Text(), nullable=True),
        sa.Column("model_key", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_git_sync_state_singleton"),
        sa.CheckConstraint(
            "(commit_sha IS NULL) = (chunker_key IS NULL)"
            " AND (commit_sha IS NULL) = (model_key IS NULL)"
            " AND (commit_sha IS NULL) = (synced_at IS NULL)",
            name="ck_git_sync_state_indexed_half",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_table(
        "chat_chunks",
        sa.Column("session_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("window_no", sa.Integer(), nullable=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["content_sha"], [f"{SCHEMA}.contents.content_sha"]),
        sa.PrimaryKeyConstraint("session_id", "window_no"),
        schema=SCHEMA,
    )
    op.create_table(
        "chat_chunk_messages",
        sa.Column("session_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("window_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("message_id", PGUUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "window_no"],
            [f"{SCHEMA}.chat_chunks.session_id", f"{SCHEMA}.chat_chunks.window_no"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "window_no", "ordinal"),
        schema=SCHEMA,
    )
    op.create_index("idx_chat_chunk_messages_message", "chat_chunk_messages", ["message_id"], schema=SCHEMA)
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chunker_key", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
        schema=SCHEMA,
    )
