"""Drop the `claude_chat_*` compatibility views. **Destructive.**

The contract half of `0040`. Those views are the only thing keeping a replica on the pre-rename
image able to read and write; dropping them makes every one of its statements fail.

**Gate this on the roll having converged** — every pod on an image at or after `0040` — not on a
release having elapsed. `maxUnavailable: 0` means a bad image stalls the roll with the old replica
still serving, so "the previous release shipped" does not imply "the previous code is gone".

The six names are spelled out here rather than imported from `0040`, for the reason `0028` gives
about the lease TTL: a migration is a point-in-time statement about the database, and reaching into
another revision would make an already-applied migration change meaning when that one is edited.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | None = None
depends_on: str | None = None

_VIEWS = {
    "claude_chat_sessions": "sessions",
    "claude_chat_messages": "session_messages",
    "claude_chat_prompts": "session_prompts",
    "claude_chat_turns": "session_turns",
    "claude_chat_turn_prompts": "session_turn_prompts",
    "claude_chat_frames": "session_frames",
}


def upgrade() -> None:
    for view in _VIEWS:
        op.execute(text(f'DROP VIEW "{view}"'))


def downgrade() -> None:
    for view, table in _VIEWS.items():
        op.execute(text(f'CREATE VIEW "{view}" AS SELECT * FROM "{table}"'))
