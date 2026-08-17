"""Say what the runtime already guarantees, now that the rows written otherwise are gone.

Every `sessions` row was deleted on 2026-08-16 (<../../debug/2026_08_16_legacy_purge.md>),
taking `session_{messages,frames,turns,events,prompts,outbox}` with it by cascade. What is left is
what the current writers put there, so four accommodations for older shapes become statements the
schema can make:

- **`sessions.projected_frame_seq` is a number, not a maybe.** "Nothing has projected yet" is `0`.
  The default is what carries a writer that never names the column — `SessionStore.create` does not
  set the attribute, so the ORM omits it from the `INSERT` — and the `UPDATE` is for the one row
  the replacement session left NULL before this ran.
- **`sessions.surface` is known**, and the room/surface pairing becomes one equivalence rather than
  two one-way rules. The pair said the same thing already; splitting it was for a legacy row that
  had neither, and there is no such row now.
- **`ck_session_messages_source_anchored` is validated.** `0046` added it `NOT VALID` so unpointed
  history could stay; that history is deleted.
- **An assistant message says where it came from.** A user row stays unpointed while its prompt is
  unclaimed — a live state, and `PromptFate.LOST` keeps it forever — so the rule is split by role
  rather than written on the column.

`ck_session_frames_runner_seq_direction` states the other half of `0050`: the runner numbers what it
puts on the wire, so a number on a frame this console sent would be a number nobody assigned.

Revision ID: 0058
Revises: 0057
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | None = None
depends_on: str | None = None

_ANCHORED = "ck_session_messages_source_anchored"
_ANCHORED_CHECK = "source_last_frame_seq IS NULL OR source_first_frame_seq IS NOT NULL"


def upgrade() -> None:
    op.execute(sa.text("UPDATE sessions SET projected_frame_seq = 0 WHERE projected_frame_seq IS NULL"))
    op.alter_column(
        "sessions", "projected_frame_seq", existing_type=sa.BigInteger(), server_default=sa.text("0"), nullable=False
    )
    op.alter_column("sessions", "surface", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("ck_sessions_surface", "sessions", type_="check")
    op.create_check_constraint("ck_sessions_surface", "sessions", "surface IN ('spa','matrix')")
    op.drop_constraint("ck_sessions_room_is_matrix", "sessions", type_="check")
    op.drop_constraint("ck_sessions_matrix_has_room", "sessions", type_="check")
    op.create_check_constraint("ck_sessions_matrix_room", "sessions", "(surface = 'matrix') = (room_id IS NOT NULL)")
    op.execute(sa.text(f"ALTER TABLE session_messages VALIDATE CONSTRAINT {_ANCHORED}"))
    op.create_check_constraint(
        "ck_session_messages_assistant_pointed",
        "session_messages",
        "role <> 'assistant' OR source_first_frame_seq IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_session_frames_runner_seq_direction", "session_frames", "runner_seq IS NULL OR direction = 'from_agent'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_session_frames_runner_seq_direction", "session_frames", type_="check")
    op.drop_constraint("ck_session_messages_assistant_pointed", "session_messages", type_="check")
    # Re-added rather than un-validated: Postgres has no statement that takes a constraint back to
    # `NOT VALID`, and the earlier revisions read it as one they may leave unenforced.
    op.drop_constraint(_ANCHORED, "session_messages", type_="check")
    op.execute(sa.text(f"ALTER TABLE session_messages ADD CONSTRAINT {_ANCHORED} CHECK ({_ANCHORED_CHECK}) NOT VALID"))
    op.drop_constraint("ck_sessions_matrix_room", "sessions", type_="check")
    op.create_check_constraint("ck_sessions_room_is_matrix", "sessions", "room_id IS NULL OR surface = 'matrix'")
    op.create_check_constraint("ck_sessions_matrix_has_room", "sessions", "surface <> 'matrix' OR room_id IS NOT NULL")
    op.drop_constraint("ck_sessions_surface", "sessions", type_="check")
    op.create_check_constraint("ck_sessions_surface", "sessions", "surface IS NULL OR surface IN ('spa','matrix')")
    op.alter_column("sessions", "surface", existing_type=sa.Text(), nullable=True)
    # The cursor stays `0` where this migration wrote it: the row it replaced said "nothing has
    # projected", which is what `0` now means, and no earlier revision reads the two differently.
    op.alter_column(
        "sessions", "projected_frame_seq", existing_type=sa.BigInteger(), server_default=None, nullable=True
    )
