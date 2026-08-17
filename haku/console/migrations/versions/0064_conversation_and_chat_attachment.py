"""A conversation is an id, and the channels holding a copy of it hang off that id.

`conversation(conversation_id, operator_id, created_at)` is identity and nothing else: every fact
stays where it already is — what was said on the session, delivery on the attachment, rendering on
the channel. What it buys is that a channel's attachment stops moving when the sandbox dies and the
session is replaced.

`chat_attachment` is keyed on the conversation rather than on the session for that same reason, and
it is for **copy-holding channels only**: an attachment row exists to hold a cursor, a cursor exists
because a channel holds a copy the console owes work against, and a browser tab holds none. So the
SPA gets no row and no synthetic address.

**The backfill is one conversation per session, except Matrix sessions grouped by `room_id`** and
ordered by `created_at`, which share one — the successive sessions that served a room always were
one conversation, and `sessions.room_id` is where that was written down. Each room's live
`chat_attachment` row comes from the same grouping.

**Additive, and safe for the length of a roll.** Nothing here is read by the previous image,
`matrix_conversation` and `sessions.{room_id,surface}` are untouched, and `sessions.conversation_id`
is **nullable**: the previous image's `INSERT INTO sessions` does not name the column, so a
`NOT NULL` would reject the first session of the roll. It is filled by every session this release
creates and takes its `NOT NULL` in the release after this one has converged, which is also when a
reader may key on it.

Revision ID: 0064
Revises: 0063
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    _create_tables()
    _backfill()


def _create_tables() -> None:
    op.create_table(
        "conversation",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_conversation_operator", "conversation", ["operator_id", "created_at"])
    op.create_table(
        "chat_attachment",
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detached_at", sa.DateTime(timezone=True), nullable=True),
        # `spa` is deliberately not admissible: a tab holds no copy, so it has no cursor to keep and
        # no address to key by. Widening this is what a second copy-holding channel does.
        sa.CheckConstraint("surface IN ('matrix')", name="ck_chat_attachment_surface"),
        sa.CheckConstraint("btrim(address) <> ''", name="ck_chat_attachment_address_nonempty"),
        sa.CheckConstraint(
            "detached_at IS NULL OR detached_at >= attached_at", name="ck_chat_attachment_detach_after_attach"
        ),
    )
    # One conversation per address at a time, and a detached one leaves the address free — which is
    # what "start this room over" is, now that a conversation never ends.
    op.create_index(
        "uq_chat_attachment_live_address",
        "chat_attachment",
        ["surface", "address"],
        unique=True,
        postgresql_where=sa.text("detached_at IS NULL"),
    )
    op.create_index("idx_chat_attachment_conversation", "chat_attachment", ["conversation_id", "attached_at"])
    op.add_column(
        "sessions",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("idx_sessions_conversation", "sessions", ["conversation_id", "created_at"])


def _backfill() -> None:
    # Temporary tables rather than data-modifying CTEs: a conversation has no natural key, so the
    # minted ids have to be readable again by whatever they were minted for, and a temp table says
    # that plainly where a chain of CTEs would rely on which of them Postgres materializes.
    op.execute(
        sa.text(
            "CREATE TEMP TABLE _conversation_per_room ON COMMIT DROP AS "
            "SELECT DISTINCT ON (room_id) "
            "  room_id, gen_random_uuid() AS conversation_id, operator_id, created_at, session_id "
            "FROM sessions WHERE room_id IS NOT NULL "
            # The room's earliest session is the one whose operator and start the conversation
            # takes; `session_id` breaks a tie `created_at` alone leaves.
            "ORDER BY room_id, created_at, session_id"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO conversation (conversation_id, operator_id, created_at) "
            "SELECT conversation_id, operator_id, created_at FROM _conversation_per_room"
        )
    )
    op.execute(
        sa.text(
            "UPDATE sessions AS s SET conversation_id = r.conversation_id "
            "FROM _conversation_per_room AS r WHERE s.room_id = r.room_id"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO chat_attachment "
            "  (attachment_id, conversation_id, surface, address, attached_at, detached_at) "
            "SELECT gen_random_uuid(), conversation_id, 'matrix', room_id, created_at, NULL "
            "FROM _conversation_per_room"
        )
    )
    op.execute(
        sa.text(
            "CREATE TEMP TABLE _conversation_per_session ON COMMIT DROP AS "
            "SELECT session_id, gen_random_uuid() AS conversation_id, operator_id, created_at "
            "FROM sessions WHERE conversation_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO conversation (conversation_id, operator_id, created_at) "
            "SELECT conversation_id, operator_id, created_at FROM _conversation_per_session"
        )
    )
    op.execute(
        sa.text(
            "UPDATE sessions AS s SET conversation_id = p.conversation_id "
            "FROM _conversation_per_session AS p WHERE s.session_id = p.session_id"
        )
    )


def downgrade() -> None:
    # `sessions.room_id` still holds the grouping this read, so re-running the upgrade rebuilds the
    # same conversations. What is lost is the identity of any conversation minted since — a room
    # detached and re-attached becomes one conversation again.
    op.drop_index("idx_sessions_conversation", table_name="sessions")
    op.drop_column("sessions", "conversation_id")
    op.drop_index("idx_chat_attachment_conversation", table_name="chat_attachment")
    op.drop_index("uq_chat_attachment_live_address", table_name="chat_attachment")
    op.drop_table("chat_attachment")
    op.drop_index("idx_conversation_operator", table_name="conversation")
    op.drop_table("conversation")
