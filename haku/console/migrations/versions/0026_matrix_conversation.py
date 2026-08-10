"""The room Haku services and the chat session bound to it.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_conversation",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("room_id", sa.Text(), nullable=False),
        sa.Column(
            "session_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("claude_chat_sessions.session_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("matrix_conversation")
