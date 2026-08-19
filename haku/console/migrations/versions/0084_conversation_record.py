"""Replace the chat tables with the conversation record.

The log becomes the only account of what happened, and the entities become folds of it. What this
removes is a second, independently written copy of the same facts — a transcript table the fold
updated in place — which could disagree with the log and had no rule saying which was right.
<../../docs/conversation_schema.md> is the design; this is the one migration that reaches it.

**Nothing is carried across.** Every existing conversation's transcript is discarded rather than
migrated, which is what makes the cut affordable and is also why it can only be done once. There is
no `ALTER` here for that reason: the new tables are created outright.

**Reap the sandbox claims before running this.** The claim sweep finds its work through `sessions`,
so a live session emptied here leaves a sandbox nobody will collect. `DELETE FROM sessions` below is
the schema half; the operational half is that the fleet must already be quiesced when it runs.

**`conversation` and `chat_attachment` survive.** Deleting conversations would cascade to every
attachment, and every Matrix room would lose its binding and need re-inviting for nothing. The
threads stay; what hangs beneath them goes.

**The chat surface is down for the length of the roll.** `maxUnavailable: 0` keeps the previous
image serving against the new schema, and it selects tables that no longer exist. That is accepted
rather than overlooked: the approval queue, agent authority, operator login, OAuth, Web Push and the
node daemons share no column with any table here and serve throughout.

Revision ID: 0084
Revises: 0083
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

# Dependents first: each of these names one of the tables after it.
_DROPPED = (
    "matrix_ingress_event",
    "session_outbox",
    "session_events",
    "session_turn_prompts",
    "session_prompts",
    "session_turns",
    "session_messages",
    "chat_delivery",
    "matrix_room_cursor",
)

_ITEM_KINDS = "'item_started','item_segment','item_completed'"


def upgrade() -> None:
    # Before the drops, so the rows that own the sandboxes are gone while the tables that describe
    # them still exist.
    op.execute(sa.text("DELETE FROM sessions"))

    for table in _DROPPED:
        op.drop_table(table)

    # The log's address is dense within a conversation, so it is handed out from a counter rather
    # than a sequence: a sequence is unique but leaves gaps, and a gap a channel cannot tell from
    # loss is what a position-based resume must not have.
    op.add_column("conversation", sa.Column("next_event_seq", sa.BigInteger(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_conversation_next_event_seq", "conversation", "next_event_seq > 0")

    op.create_table(
        "conversation_turn",
        sa.Column("turn_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=True),
        sa.Column("first_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("last_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("turn_id", name="conversation_turn_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_turn_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="conversation_turn_session_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','aborted','failed')", name="ck_conversation_turn_outcome"
        ),
        sa.CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ck_conversation_turn_ended"),
        sa.CheckConstraint("(ended_at IS NULL) = (last_seq IS NULL)", name="ck_conversation_turn_last_seq"),
        sa.CheckConstraint("last_seq IS NULL OR last_seq >= first_seq", name="ck_conversation_turn_seq_order"),
    )
    # One open turn per *conversation*, not per session: "only one session holds a conversation at a
    # time" is a conversation-layer rule, so the index enforcing it belongs on the conversation.
    op.create_index(
        "uq_conversation_turn_open",
        "conversation_turn",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index("idx_conversation_turn_conversation", "conversation_turn", ["conversation_id", "first_seq"])
    op.create_index("idx_conversation_turn_session", "conversation_turn", ["session_id", "first_seq"])

    op.create_table(
        "conversation_item",
        sa.Column("item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("turn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("opened_seq", sa.BigInteger(), nullable=False),
        sa.Column("closed_seq", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("backend_item_id", sa.Text(), nullable=True),
        sa.Column("origin", JSONB(), nullable=True),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("arguments", JSONB(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("structured", JSONB(), nullable=True),
        sa.Column("disclosure", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("item_id", name="conversation_item_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_item_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="conversation_item_session_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turn.turn_id"], name="conversation_item_turn_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "item_type IN ('prompt','message','reasoning','tool_call')", name="ck_conversation_item_type"
        ),
        sa.CheckConstraint("status IN ('open','complete','failed')", name="ck_conversation_item_status"),
        sa.CheckConstraint("(status = 'open') = (closed_seq IS NULL)", name="ck_conversation_item_open"),
        sa.CheckConstraint(
            "closed_seq IS NULL OR closed_seq >= opened_seq", name="ck_conversation_item_close_after_open"
        ),
        sa.CheckConstraint(
            "(item_type = 'tool_call') = (call_id IS NOT NULL) "
            "AND (item_type = 'tool_call') = (tool_name IS NOT NULL) "
            "AND (item_type = 'tool_call' OR arguments IS NULL) "
            "AND (item_type = 'tool_call' OR outcome IS NULL) "
            "AND (item_type = 'tool_call' OR structured IS NULL)",
            name="ck_conversation_item_tool_call_fields",
        ),
        sa.CheckConstraint(
            "(item_type = 'reasoning' OR disclosure IS NULL) AND (item_type = 'prompt' OR origin IS NULL)",
            name="ck_conversation_item_typed_fields",
        ),
        sa.CheckConstraint(
            "status <> 'complete' OR ((item_type <> 'tool_call' OR outcome IS NOT NULL) "
            "AND (item_type <> 'reasoning' OR disclosure IS NOT NULL))",
            name="ck_conversation_item_complete_terminal_fields",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded','failed','unknown')", name="ck_conversation_item_outcome"
        ),
        sa.CheckConstraint(
            "disclosure IS NULL OR disclosure IN ('summary','withheld')", name="ck_conversation_item_disclosure"
        ),
    )
    op.create_index(
        "uq_conversation_item_call",
        "conversation_item",
        ["conversation_id", "call_id"],
        unique=True,
        postgresql_where=sa.text("call_id IS NOT NULL"),
    )
    op.create_index("idx_conversation_item_conversation", "conversation_item", ["conversation_id", "opened_seq"])
    op.create_index("idx_conversation_item_turn", "conversation_item", ["turn_id", "opened_seq"])

    op.create_table(
        "conversation_event",
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("turn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("source_first_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("source_last_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("body", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id", "event_seq", name="conversation_event_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_event_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="conversation_event_session_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turn.turn_id"], name="conversation_event_turn_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["conversation_item.item_id"], name="conversation_event_item_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint("event_seq > 0", name="ck_conversation_event_seq_positive"),
        sa.CheckConstraint("provenance IN ('frame_range','authored')", name="ck_conversation_event_provenance"),
        sa.CheckConstraint(
            "(provenance = 'frame_range') = (source_first_frame_seq IS NOT NULL) "
            "AND (source_first_frame_seq IS NULL) = (source_last_frame_seq IS NULL) "
            "AND (source_first_frame_seq IS NULL OR source_first_frame_seq <= source_last_frame_seq) "
            "AND (provenance <> 'frame_range' OR turn_id IS NOT NULL) "
            "AND (provenance <> 'frame_range' OR session_id IS NOT NULL) "
            "AND (provenance <> 'frame_range' OR item_id IS NOT NULL)",
            name="ck_conversation_event_provenance_frames",
        ),
        # What replaces `0082`'s kind-to-arm pin: an item kind may take either arm — a prompt is
        # authored, an assistant message is folded — so the arm follows from
        # `conversation_item.item_type`, which this table cannot see, and the kind states only
        # whether an item is named at all.
        sa.CheckConstraint(
            f"(item_id IS NOT NULL) = (kind IN ({_ITEM_KINDS}))", name="ck_conversation_event_item_kinds"
        ),
    )
    op.create_index("idx_conversation_event_session", "conversation_event", ["session_id", "event_seq"])
    op.create_index("idx_conversation_event_item", "conversation_event", ["item_id", "event_seq"])

    op.create_table(
        "conversation_prompt",
        sa.Column("prompt_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by_session_id", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("prompt_id", name="conversation_prompt_pkey"),
        sa.UniqueConstraint("item_id", name="uq_conversation_prompt_item"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_prompt_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["conversation_item.item_id"], name="conversation_prompt_item_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turn.turn_id"], name="conversation_prompt_turn_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by_session_id"],
            ["sessions.session_id"],
            name="conversation_prompt_claimed_by_session_id_fkey",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claimed_by_session_id IS NULL)", name="ck_conversation_prompt_claim"
        ),
        sa.CheckConstraint(
            "claimed_at IS NULL OR claimed_at >= queued_at", name="ck_conversation_prompt_claim_after_queue"
        ),
    )
    op.create_index(
        "uq_conversation_prompt_unclaimed",
        "conversation_prompt",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("claimed_at IS NULL"),
    )
    op.create_index("idx_conversation_prompt_conversation", "conversation_prompt", ["conversation_id", "queued_at"])

    # The one channel-generic piece of channel state: a position in the log is the resume contract
    # every attachment owes the conversation layer. Everything below it is one channel's own.
    op.create_table(
        "channel_cursor",
        sa.Column("attachment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("attachment_id", name="channel_cursor_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["chat_attachment.attachment_id"],
            name="channel_cursor_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("event_seq >= 0", name="ck_channel_cursor_event_seq"),
    )

    op.create_table(
        "matrix_revision",
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("revision_id", name="matrix_revision_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["chat_attachment.attachment_id"],
            name="matrix_revision_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_matrix_revision_subject_nonempty"),
        sa.CheckConstraint("btrim(event_id) <> ''", name="ck_matrix_revision_event_nonempty"),
        sa.CheckConstraint("retired_at IS NULL OR retired_at >= sent_at", name="ck_matrix_revision_retire_after_sent"),
    )
    op.create_index(
        "uq_matrix_revision_live_subject",
        "matrix_revision",
        ["attachment_id", "subject"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )

    op.create_table(
        "matrix_outbox",
        sa.Column("outbox_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("outbox_id", name="matrix_outbox_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["chat_attachment.attachment_id"],
            name="matrix_outbox_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_matrix_outbox_subject_nonempty"),
    )
    op.create_index(
        "idx_matrix_outbox_unsent",
        "matrix_outbox",
        ["attachment_id", "created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_index("uq_matrix_outbox_subject", "matrix_outbox", ["attachment_id", "subject"], unique=True)

    # Re-pointed at the prompt item: a prompt is an item like any other now, so ingress dedupes
    # against the transcript rather than against a separate message table.
    op.create_table(
        "matrix_ingress_event",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("item_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="matrix_ingress_event_pkey"),
        sa.ForeignKeyConstraint(
            ["item_id"], ["conversation_item.item_id"], name="matrix_ingress_event_item_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index("idx_matrix_ingress_event_item", "matrix_ingress_event", ["item_id"])


def downgrade() -> None:
    raise RuntimeError("0084 discards the transcript it replaces; there is nothing to restore")
