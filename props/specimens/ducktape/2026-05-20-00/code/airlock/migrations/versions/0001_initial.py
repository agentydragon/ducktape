"""Initial schema: actions, action_seq_counters, event_log, and kv_store tables.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("session_key", sa.Uuid(), primary_key=True),
        sa.Column("action_seq", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("call_json", sa.Text(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=True),
    )
    op.create_index("idx_actions_status", "actions", ["status"])
    op.create_index("idx_actions_created", "actions", ["created_at"])
    op.create_table(
        "action_seq_counters",
        sa.Column("session_key", sa.Uuid(), primary_key=True),
        sa.Column("next_seq", sa.Integer(), nullable=False),
    )
    op.create_table(
        "event_log",
        sa.Column("session_key", sa.Uuid(), primary_key=True),
        sa.Column("entry_id", sa.Integer(), primary_key=True),
        sa.Column("action_seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_log_session_action", "event_log", ["session_key", "action_seq"])
    op.create_table(
        "kv_store",
        sa.Column("collection", sa.String(), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("kv_store")
    op.drop_table("event_log")
    op.drop_table("action_seq_counters")
    op.drop_table("actions")
