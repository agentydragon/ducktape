"""Drop what the last three releases stopped writing. **Destructive.**

The contract half of four separate unmappings, which converged together. Each is gated on every
pod running an image at or after the release that stopped writing the thing — not on a release
having elapsed — because an ORM-mapped column is named in every `SELECT` SQLAlchemy emits for it,
so a replica still on the mapping image would fail on every statement the moment this runs. All
four gates were read off one deployment (`devel-20260817092449-3da90ff`, both replicas on one tag):

- `session_messages.{tool_calls,unpointable_reason}` and the two `unpointable_*` constraints,
  unmapped by #4266 — <../../plans/next_month.md> § 1 phase 3.
- `session_frames.partial` and `uq_session_frames_partial`, unmapped by #4277, a release behind the
  other two. The rows it marked go with it: they outlived their writer (#4230), and until they are
  deleted the fold reads them as ordinary `assistant` frames.
- `ConversationEventKind.ACTIVITY_{STARTED,COMPLETED}`, whose last writer went in #4279 — step 12
  of the conversation-layers plan, which is still on its own branch.
- The `tool_references` and `opaque` spellings of a stored tool result's content, whose writer went
  in #4284.

**Two orderings matter, in one direction each.** The activity rows are deleted before the CHECK
narrows, and the `partial` rows before the column goes — a CHECK cannot be added over rows that
violate it, and a `WHERE partial` cannot be run once the column is gone.

**The result bodies are rewritten, not deleted.** Destroying conversation data is authorized
(operator, 2026-08-17), but a delete here would blank an old session's tool results for no gain:
`ToolResultBody` is parsed on every SPA read, so the arms have to go, and rewriting each row to the
`text` shape it would carry today costs one statement and keeps the transcript readable. What the
rewrite stores is exactly what `session_views._rendered` used to compute at read time.

The column types are spelled out below rather than imported from `0030`/`0047`/`0055`, for the
reason `0041` gives: a migration is a point-in-time statement about the database, and reaching into
another revision would make an already-applied migration change meaning when that one is edited.

Revision ID: 0068
Revises: 0067
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | None = None
depends_on: str | None = None

_EVENTS = "session_events"
_KIND = "ck_session_events_kind"
_MESSAGES = "session_messages"
_FRAMES = "session_frames"

_UNPOINTABLE_VALUE = "ck_session_messages_unpointable_reason"
_UNPOINTABLE_EXCLUSIVE = "ck_session_messages_unpointable_exclusive"

_ACTIVITY_KINDS = "'activity_started','activity_completed'"
_KEPT_KINDS = (
    "'message_completed','reasoning','tool_call_started','tool_call_completed',"
    "'prompt_enqueued','session_adopted','lease_expired','turn_aborted',"
    "'prompt_rejected','unreadable_input'"
)


def upgrade() -> None:
    op.execute(sa.text(f"DELETE FROM {_EVENTS} WHERE kind IN ({_ACTIVITY_KINDS})"))
    op.drop_constraint(_KIND, _EVENTS, type_="check")
    op.create_check_constraint(_KIND, _EVENTS, f"kind IN ({_KEPT_KINDS})")

    # Both arms rendered as JSON at read time, so the text each row keeps is what the SPA was
    # already showing for it.
    for shape, carried in (("tool_references", "tool_names"), ("opaque", "payload")):
        op.execute(
            sa.text(
                f"UPDATE {_EVENTS} SET body = jsonb_set(body, '{{content}}', jsonb_build_object("
                f"'shape', 'text', 'text', (body->'content'->'{carried}')::text)) "
                f"WHERE body->'content'->>'shape' = '{shape}'"
            )
        )

    op.drop_constraint(_UNPOINTABLE_EXCLUSIVE, _MESSAGES, type_="check")
    op.drop_constraint(_UNPOINTABLE_VALUE, _MESSAGES, type_="check")
    op.drop_column(_MESSAGES, "unpointable_reason")
    op.drop_column(_MESSAGES, "tool_calls")

    op.execute(sa.text(f"DELETE FROM {_FRAMES} WHERE partial"))
    op.drop_index("uq_session_frames_partial", table_name=_FRAMES)
    op.drop_column(_FRAMES, "partial")


def downgrade() -> None:
    """Puts the shapes back, not what they held.

    The deleted rows are gone, and a result body rewritten to `text` no longer says which spelling
    it arrived as. Both are the point of the upgrade rather than an oversight, so this restores the
    columns, the index and the constraints an earlier image expects to find and nothing else.
    """
    op.add_column(_FRAMES, sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index(
        "uq_session_frames_partial", _FRAMES, ["session_id"], unique=True, postgresql_where=sa.text("partial")
    )

    op.add_column(
        _MESSAGES,
        sa.Column(
            "tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(_MESSAGES, sa.Column("unpointable_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        _UNPOINTABLE_VALUE,
        _MESSAGES,
        "unpointable_reason IS NULL "
        "OR unpointable_reason IN ('no_matching_projection','ambiguous_text','out_of_order')",
    )
    op.create_check_constraint(
        _UNPOINTABLE_EXCLUSIVE, _MESSAGES, "unpointable_reason IS NULL OR source_first_frame_seq IS NULL"
    )

    op.drop_constraint(_KIND, _EVENTS, type_="check")
    op.create_check_constraint(_KIND, _EVENTS, f"kind IN ({_KEPT_KINDS},{_ACTIVITY_KINDS})")
