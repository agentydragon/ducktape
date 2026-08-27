"""Record why a failed turn failed, beside the outcome that says it did.

A failed turn carried an outcome and nothing else, so the reason the runtime gave reached
`sessions.error` and no further and the conversation protocol could not state it (#4752).

`ck_conversation_turn_failure` is what makes "a failure states its reason" true rather than
intended: the column is set exactly when the outcome is `failed`.

Existing closed turns have no reason to backfill — it was never recorded — so the conversation
tables are emptied instead, which `haku/console/AGENTS.md` § "Conversation data may be dropped"
permits while the console is in development. Everything conversation-scoped cascades from
`conversation`. The tool-call ledger is preserved, not untouched: session-attributed principal
rows drop their `session_id` — a pointer into the deleted sessions, whose RESTRICT foreign key
would otherwise veto the cascade — while the rows themselves and their `operator_id`/`binding_id`
principal identity survive. Identity, credential and grant tables are untouched.

Revision ID: 0096
Revises: 0095
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "ck_conversation_turn_failure"


def upgrade() -> None:
    # `fk_mcp_tool_call_principals_session_binding` is ON DELETE RESTRICT, so a session-attributed
    # audit row would veto the sessions cascade below. Clear the session pointer instead of the row:
    # `session_id` is nullable by design (0093) and the composite FK is MATCH SIMPLE, so a NULL
    # detaches it while `binding_id` keeps satisfying its own FK to `credential_bindings`.
    # `trg_haku_0009_tool_call_principal_immutable` permits this: it pins exactly
    # (tool_call_id, operator_id, binding_id), which this update leaves untouched.
    op.execute(sa.text("UPDATE mcp_tool_call_principals SET session_id = NULL WHERE session_id IS NOT NULL"))
    # Cascades to chat_attachment, sessions, conversation_event, conversation_item,
    # conversation_turn, conversation_prompt and session_frames.
    op.execute(sa.text("DELETE FROM conversation"))
    op.add_column("conversation_turn", sa.Column("failure", sa.Text(), nullable=True))
    op.create_check_constraint(
        _CONSTRAINT, "conversation_turn", "(failure IS NULL) = (outcome IS DISTINCT FROM 'failed')"
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "conversation_turn", type_="check")
    op.drop_column("conversation_turn", "failure")
