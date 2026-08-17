"""What a channel put in its own copy of a conversation, and where it put it.

No table held a Matrix `event_id`: `post_reply` discarded the one the homeserver returned and the
status line's id was an instance attribute, so which room event shows which recorded thing could
only be recovered by reading the room back and parsing the tag off every event
(<../../plans/session_channels.md> § 1). `chat_delivery` stores that correspondence beside the
attachment whose channel wrote it — one row per `(attachment, subject)` the channel still shows,
with the subject and the reference both opaque outside that channel.

**Additive, and safe for the length of a roll** (<../../README.md> § Perimeter / deploy). A new
table nothing else references: the previous image neither writes it nor joins through it, and the
`chat_attachment` rows it hangs off already exist.

Revision ID: 0067
Revises: 0066
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "chat_delivery",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "attachment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_attachment.attachment_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sent_ref", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_chat_delivery_subject_nonempty"),
        sa.CheckConstraint("btrim(sent_ref) <> ''", name="ck_chat_delivery_ref_nonempty"),
        sa.CheckConstraint("retired_at IS NULL OR retired_at >= sent_at", name="ck_chat_delivery_retire_after_sent"),
    )
    # One live event per subject, which is what makes re-deriving a subject find the event already
    # showing it rather than send a second one. Retiring a row frees the subject for the next.
    op.create_index(
        "uq_chat_delivery_live_subject",
        "chat_delivery",
        ["attachment_id", "subject"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_chat_delivery_live_subject", table_name="chat_delivery")
    op.drop_table("chat_delivery")
