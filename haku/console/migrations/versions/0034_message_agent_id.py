"""An assistant message records the agent message it came from, so its tool calls are findable.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive and nullable. NULL means "no id was recorded": a row written by a replica on the
    # previous image, or one this console synthesized rather than observed — a turn whose text
    # arrived only on the `result` frame. Both read their tool calls from the `tool_uses` column,
    # which is still written, so this column is a pointer that improves what a reader can find
    # rather than one anything yet depends on.
    op.add_column("claude_chat_messages", sa.Column("agent_message_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claude_chat_messages", "agent_message_id")
