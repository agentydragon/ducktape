"""Index the conversation item read's keyset branches.

`read_items` pages `conversation_item` and `conversation_turn` by the rows' defining stream
positions — a tool call's `opened_seq`, a completed item's `closed_seq`, an ended turn's
`last_seq` — so a page's cost is the page's own rows. These partial indexes are what lets the
planner serve each branch as a keyset walk rather than filtering or sorting the conversation's
whole row set per page.

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "idx_conversation_item_tool_call_opened",
        "conversation_item",
        ["conversation_id", "opened_seq"],
        postgresql_where=sa.text("item_type = 'tool_call'"),
    )
    op.create_index(
        "idx_conversation_item_completed",
        "conversation_item",
        ["conversation_id", "closed_seq"],
        postgresql_where=sa.text("status = 'complete'"),
    )
    op.create_index(
        "idx_conversation_turn_ended",
        "conversation_turn",
        ["conversation_id", "last_seq"],
        postgresql_where=sa.text("last_seq IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_conversation_turn_ended", table_name="conversation_turn")
    op.drop_index("idx_conversation_item_completed", table_name="conversation_item")
    op.drop_index("idx_conversation_item_tool_call_opened", table_name="conversation_item")
