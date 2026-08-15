"""Rename the chat corpus's window ordinal to say what it is.

`chat_chunks.chunk_no` is a window's position in its session — unrelated to the blob-span ordinal
that `chunks.chunk_no` was before 0037 dropped it. The shared name was accident, and this renames
the survivor rather than leaving two meanings behind one word.

**This exists because 0037 was edited in place after it had already been applied.** #4052 renamed
the column inside 0037's `create_table`, which is a no-op for the deployed database — Alembic had
stamped 0037 and will never run it again — so production kept `chunk_no` while the ORM moved to
`window_no`. 0037 is restored here to exactly what production applied, and the rename becomes this
migration, so one revision means one schema again.

**This one is not backward compatible for the length of a roll**, which every other migration here
is. The previous release selects `w.chunk_no`, so while both versions are running its conversations
search and chat sweep error out. That is accepted rather than split into expand/contract because
the cost is bounded and small: the tables are derived state rebuilt from `claude_chat_messages` on
the next sweep, the sweep retries on its own tick, the `haku_state` corpus is untouched, and
`haku_index` is one release old with no dependents. The alternative — add, dual-write, backfill,
read, drop — is three releases of machinery for a name.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA = "state_index"


def upgrade() -> None:
    # The foreign key follows the rename on its own: Postgres tracks the constraint by column
    # identity, not by name.
    op.alter_column("chat_chunks", "chunk_no", new_column_name="window_no", schema=SCHEMA)
    op.alter_column("chat_chunk_messages", "chunk_no", new_column_name="window_no", schema=SCHEMA)
