"""Record a message's tool calls in the conversation vocabulary, not Anthropic's.

`tool_uses` stored `{tool_use_id, name, input}` — Claude's wire spelling — so a second backend
would have had to impersonate Claude to record a call its agent made. `tool_calls` stores
`{call_id, tool_name, arguments}` instead.

**Additive on purpose.** `maxUnavailable: 0` keeps a replica on the previous image SELECTing
`tool_uses` for the length of the roll, so both columns exist through one release: this one copies
the old rows forward and leaves the originals exactly as they were, and `tool_uses` is dropped in
the release after. The new column carries a server default so that an old replica's INSERT, which
does not name it, still satisfies NOT NULL.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "session_messages",
        sa.Column(
            "tool_calls", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    # Rewritten key by key rather than copied: a backfill carrying the old keys across would leave
    # every historical row unreadable by the model that validates the new ones. Rows whose calls
    # are already empty are skipped.
    op.execute(
        sa.text(
            """
            UPDATE session_messages
            SET tool_calls = (
                SELECT coalesce(
                    jsonb_agg(
                        jsonb_build_object(
                            'call_id', call ->> 'tool_use_id',
                            'tool_name', call ->> 'name',
                            'arguments', coalesce(call -> 'input', '{}'::jsonb)
                        )
                        ORDER BY ordinality
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(tool_uses) WITH ORDINALITY AS element(call, ordinality)
            )
            WHERE jsonb_array_length(tool_uses) > 0
            """
        )
    )


def downgrade() -> None:
    op.drop_column("session_messages", "tool_calls")
