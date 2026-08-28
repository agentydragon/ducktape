"""Rename the stored event kinds `item_started` and `turn_started` to the neutral-op verbs.

The #4772 vocabulary collapse (naming_and_layout.md §3.1) aligns the conversation-event
vocabulary to the neutral-operation protocol: items are opened / segment / completed, a turn is
opened and ended — no `started`. The kinds are stored strings, so the rows rename with the enum
members, and `ck_conversation_event_item_kinds` restates the item kinds it names.

A rewrite of two stored strings, not a rebuild. The conversation-tables-droppable allowance would
permit deleting the rows instead, but the in-place UPDATE preserves them for free and the schema
is the same either way. Old replicas of a roll read the renamed kinds as the tolerant unknown arm
for the roll's length — narration's named compatibility cost (README § Vocabularies across a roll).

Revision ID: 0111
Revises: 0109
"""

from __future__ import annotations

from alembic import op

revision: str = "0111"
down_revision: str | None = "0109"
branch_labels: str | None = None
depends_on: str | None = None

_ITEM_KINDS_CHECK = "ck_conversation_event_item_kinds"
_RENAMES = (("item_started", "item_opened"), ("turn_started", "turn_opened"))


def _item_kinds_check(opened: str) -> str:
    return f"(item_id IS NOT NULL) = (kind IN ('{opened}','item_segment','item_completed'))"


def upgrade() -> None:
    op.drop_constraint(_ITEM_KINDS_CHECK, "conversation_event")
    for old, new in _RENAMES:
        op.execute(f"UPDATE conversation_event SET kind = '{new}' WHERE kind = '{old}'")
    op.create_check_constraint(_ITEM_KINDS_CHECK, "conversation_event", _item_kinds_check("item_opened"))


def downgrade() -> None:
    op.drop_constraint(_ITEM_KINDS_CHECK, "conversation_event")
    for old, new in _RENAMES:
        op.execute(f"UPDATE conversation_event SET kind = '{old}' WHERE kind = '{new}'")
    op.create_check_constraint(_ITEM_KINDS_CHECK, "conversation_event", _item_kinds_check("item_started"))
