"""The room's durable copy of projected conversation events.

`matrix_room_copy` records, per Haku-authored room event, the conversation event its tag names —
written from the events' own `/sync` echoes and read by the room's reconciler before it sends, so
source correspondence outlives Synapse's transaction cache. Channel state keyed by the attachment,
cascading with the conversation like the rest of the per-attachment tables.

Revision ID: 0099
Revises: 0096
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0099"
down_revision: str | None = "0096"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_room_copy",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("attachment_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("replaces_event_id", sa.Text(), nullable=True),
        sa.Column("origin_server_ts", sa.BigInteger(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="matrix_room_copy_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["chat_attachment.attachment_id"],
            name="matrix_room_copy_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("btrim(event_id) <> ''", name="ck_matrix_room_copy_event_nonempty"),
        sa.CheckConstraint("source_event_seq > 0", name="ck_matrix_room_copy_source_positive"),
    )
    op.create_index("idx_matrix_room_copy_source", "matrix_room_copy", ["attachment_id", "source_event_seq"])


def downgrade() -> None:
    op.drop_index("idx_matrix_room_copy_source", table_name="matrix_room_copy")
    op.drop_table("matrix_room_copy")
