"""Console-side state for the neutral-operation journal (#4667 stage 3).

Expand-only, ahead of the maintenance-gated generation cut that starts writing it — no generation
flip, no data change, and every addition is inert until stage 4 activates the journal consumer:

- ``sessions.acked_batch_seq``: the per-session committed-batch cursor ACKs and resumes answer
  from; ``0`` is "nothing committed", as ``projected_frame_seq`` spells its own zero.
- ``conversation_item.runner_item_id`` / ``conversation_turn.runner_turn_id``: the runner-minted
  identities journal operations address rows by, unique per session where present.
- ``submitted_prompt``: the durable prompt inbox — text and origin authoritative in the Console
  from acceptance until ``prompt.admitted`` materialises the transcript item from the row.

Revision ID: 0106
Revises: 0105
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sessions", sa.Column("acked_batch_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column("conversation_item", sa.Column("runner_item_id", UUID(as_uuid=True), nullable=True))
    op.create_index(
        "uq_conversation_item_runner",
        "conversation_item",
        ["session_id", "runner_item_id"],
        unique=True,
        postgresql_where=sa.text("runner_item_id IS NOT NULL"),
    )
    op.add_column("conversation_turn", sa.Column("runner_turn_id", UUID(as_uuid=True), nullable=True))
    op.create_index(
        "uq_conversation_turn_runner",
        "conversation_turn",
        ["session_id", "runner_turn_id"],
        unique=True,
        postgresql_where=sa.text("runner_turn_id IS NOT NULL"),
    )
    op.create_table(
        "submitted_prompt",
        sa.Column("prompt_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("origin", JSONB(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_item_id", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("prompt_id", name="submitted_prompt_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="submitted_prompt_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["admitted_item_id"],
            ["conversation_item.item_id"],
            name="submitted_prompt_admitted_item_id_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("admitted_item_id", name="uq_submitted_prompt_admitted_item"),
        sa.CheckConstraint("btrim(text) <> ''", name="ck_submitted_prompt_text_nonempty"),
        sa.CheckConstraint("admitted_at IS NULL OR withdrawn_at IS NULL", name="ck_submitted_prompt_single_outcome"),
        sa.CheckConstraint(
            "(admitted_at IS NULL) = (admitted_item_id IS NULL)", name="ck_submitted_prompt_admission_pair"
        ),
        sa.CheckConstraint(
            "admitted_at IS NULL OR admitted_at >= submitted_at", name="ck_submitted_prompt_admit_after_submit"
        ),
        sa.CheckConstraint(
            "withdrawn_at IS NULL OR withdrawn_at >= submitted_at", name="ck_submitted_prompt_withdraw_after_submit"
        ),
    )
    op.create_index("idx_submitted_prompt_conversation", "submitted_prompt", ["conversation_id", "submitted_at"])


def downgrade() -> None:
    op.drop_table("submitted_prompt")
    op.drop_index("uq_conversation_turn_runner", table_name="conversation_turn")
    op.drop_column("conversation_turn", "runner_turn_id")
    op.drop_index("uq_conversation_item_runner", table_name="conversation_item")
    op.drop_column("conversation_item", "runner_item_id")
    op.drop_column("sessions", "acked_batch_seq")
