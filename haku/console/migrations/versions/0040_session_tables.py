"""The chat runtime's tables are named for a session, not for one backend.

`claude_chat_*` named six backend-neutral concepts after the one CLI that happens to fill them,
while the design requires a second backend to be representable. The tables become `sessions` and
`session_*`; `matrix_*` is untouched, and so is everything that genuinely names a Claude runner.

**Expand half of an expand/contract.** The Deployment rolls with `maxUnavailable: 0`, so a replica
on the previous image keeps selecting and inserting `claude_chat_*` for the length of the roll. A
bare `ALTER TABLE … RENAME` would break every one of those statements. Each old name is therefore
re-created as an **auto-updatable view** over its renamed table: one table in the `FROM`, no
aggregate, no `DISTINCT`, so Postgres rewrites the old code's `INSERT`/`UPDATE`/`DELETE` — and its
`ON CONFLICT` inference and `SELECT … FOR UPDATE` — onto the base table. Old and new code read and
write the same rows for the length of the roll. `test_session_table_compatibility.py` is what
proves that rather than assuming it. The views are dropped in the contract migration.

Only the constraints and indexes the ORM *declares* are renamed. The names Postgres assigned
itself (`claude_chat_sessions_pkey`, `…_session_id_fkey`, the frame sequence) follow their table
and are left alone: nothing in the codebase names them, `compare_metadata` does not compare them,
and every extra rename here is another statement that can fail against a production database whose
auto-names are assumed rather than declared.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = {
    "claude_chat_sessions": "sessions",
    "claude_chat_messages": "session_messages",
    "claude_chat_prompts": "session_prompts",
    "claude_chat_turns": "session_turns",
    "claude_chat_turn_prompts": "session_turn_prompts",
    "claude_chat_frames": "session_frames",
}

# `ALTER INDEX … RENAME` covers plain and unique indexes; a CHECK constraint is not an index and
# needs `ALTER TABLE … RENAME CONSTRAINT`, which is why the two lists are separate. Both are keyed
# by the table the object now lives on.
_INDEXES = {
    "idx_claude_chat_sessions_operator": "idx_sessions_operator",
    "idx_claude_chat_sessions_expired_lease": "idx_sessions_expired_lease",
    "idx_claude_chat_messages_session_created": "idx_session_messages_session_created",
    "idx_claude_chat_prompts_session": "idx_session_prompts_session",
    "uq_claude_chat_prompts_unclaimed": "uq_session_prompts_unclaimed",
    "idx_claude_chat_turns_session": "idx_session_turns_session",
    "uq_claude_chat_turns_open": "uq_session_turns_open",
    "idx_claude_chat_frames_session": "idx_session_frames_session",
    "idx_claude_chat_frames_kind": "idx_session_frames_kind",
    "uq_claude_chat_frames_uid": "uq_session_frames_uid",
    "uq_claude_chat_frames_partial": "uq_session_frames_partial",
}

_CONSTRAINTS = {
    "sessions": {
        "ck_claude_chat_sessions_status": "ck_sessions_status",
        "ck_claude_chat_sessions_surface": "ck_sessions_surface",
        "ck_claude_chat_sessions_room_is_matrix": "ck_sessions_room_is_matrix",
        "ck_claude_chat_sessions_matrix_has_room": "ck_sessions_matrix_has_room",
    },
    "session_messages": {
        "ck_claude_chat_messages_role": "ck_session_messages_role",
        "ck_claude_chat_messages_status": "ck_session_messages_status",
    },
    "session_turns": {
        "ck_claude_chat_turns_outcome": "ck_session_turns_outcome",
        "ck_claude_chat_turns_ended_has_outcome": "ck_session_turns_ended_has_outcome",
    },
    "session_frames": {"ck_claude_chat_frames_direction": "ck_session_frames_direction"},
}


def upgrade() -> None:
    for old, new in TABLES.items():
        op.rename_table(old, new)
    for old_index, new_index in _INDEXES.items():
        op.execute(text(f'ALTER INDEX "{old_index}" RENAME TO "{new_index}"'))
    for table, renames in _CONSTRAINTS.items():
        for old_constraint, new_constraint in renames.items():
            op.execute(text(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old_constraint}" TO "{new_constraint}"'))
    for old, new in TABLES.items():
        # `SELECT *` is expanded at creation, so the view's column list is fixed here rather than
        # tracking later ALTERs — which is what is wanted: it exists to serve exactly the columns
        # the previous release knows about.
        op.execute(text(f'CREATE VIEW "{old}" AS SELECT * FROM "{new}"'))


def downgrade() -> None:
    for old in TABLES:
        op.execute(text(f'DROP VIEW "{old}"'))
    for table, renames in _CONSTRAINTS.items():
        for old_constraint, new_constraint in renames.items():
            op.execute(text(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{new_constraint}" TO "{old_constraint}"'))
    for old_index, new_index in _INDEXES.items():
        op.execute(text(f'ALTER INDEX "{new_index}" RENAME TO "{old_index}"'))
    for old, new in TABLES.items():
        op.rename_table(new, old)
