"""Give the haku index a home in the console's database.

Additive and in its own `state_index` schema: nothing the console already serves reads these
tables, so an old replica running through the roll is unaffected, and the index is derived state
that can be dropped and rebuilt from git and `claude_chat_messages` at any time.

**The `vector` extension is a precondition, not something this migration installs.** pgvector is
untrusted, so creating it needs superuser and this runs as `approval_store`. CNPG's `Database` CR
declares it (<../../../../cluster/k8s/haku/console/db/approval-store-database.yaml>), which is why
`store.ensure_schema` still creates it for the CLI and the tests — those own their whole database —
and this does not. If the extension is missing, this migration fails and the new replica never
becomes Ready; the Deployment's `maxUnavailable: 0` leaves the running version serving.

The tables are declared in `haku/state_index/schema.py`; `test_state_index_migration.py` is what
holds the two definitions together.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from haku.state_index.vector_type import Vector

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA = "state_index"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "chunks",
        sa.Column("corpus", sa.Text(), nullable=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("chunker_key", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("byte_start", sa.BigInteger(), nullable=False),
        sa.Column("byte_end", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # No typmod: the dimension is a property of `model_key`, and pinning it here would make
        # changing the embedding model a migration. Nothing indexes it — searches are exact KNN
        # over a filtered set at this corpus size.
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("corpus", "content_sha", "chunk_no", "chunker_key", "model_key"),
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
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("chunker_key", sa.Text(), nullable=False),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_git_sync_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )

    op.create_table(
        "chat_chunks",
        sa.Column("session_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "chunk_no"),
        schema=SCHEMA,
    )

    op.create_table(
        "chat_chunk_messages",
        sa.Column("session_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("message_id", PGUUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "chunk_no"],
            [f"{SCHEMA}.chat_chunks.session_id", f"{SCHEMA}.chat_chunks.chunk_no"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "chunk_no", "ordinal"),
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
