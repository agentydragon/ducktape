"""Rename chat_attachment to channel_attachment, with its constraints and indexes.

The #4772 vocabulary collapse (naming_and_layout.md §3.1) retires "chat" from the layer
vocabulary: the channel-copy concept is `ChannelAttachment` / `channel_attachment`, and the
front-end kind enum is `ChannelSurface` with its forbidden `spa` member dropped. The surface CHECK
admitted only `matrix` already, so no `spa` row can exist and dropping the enum member changes no
data.

A rename, not a rebuild: `ALTER TABLE … RENAME` carries the rows, and the foreign keys in
`channel_cursor`, `matrix_revision`, `matrix_room_copy`, and `matrix_outbox` follow the table by
OID. The constraint and index names are renamed alongside so the migrated schema matches the ORM.
The conversation-tables-droppable allowance would permit a drop instead (channel_attachment
cascades from conversation), but the rename preserves the rows for free and is the smaller change.

Revision ID: 0108
Revises: 0106
"""

from __future__ import annotations

from alembic import op

revision: str = "0108"
down_revision: str | None = "0106"
branch_labels: str | None = None
depends_on: str | None = None

# Objects named after the table, renamed with it so the schema matches the ORM's implicit and
# explicit names. Primary key and the conversation foreign key are unnamed in the ORM, so Postgres
# gives them the `<table>_pkey` / `<table>_<col>_fkey` defaults the new names spell.
_CONSTRAINTS = (
    ("chat_attachment_pkey", "channel_attachment_pkey"),
    ("chat_attachment_conversation_id_fkey", "channel_attachment_conversation_id_fkey"),
    ("ck_chat_attachment_surface", "ck_channel_attachment_surface"),
    ("ck_chat_attachment_address_nonempty", "ck_channel_attachment_address_nonempty"),
    ("ck_chat_attachment_detach_after_attach", "ck_channel_attachment_detach_after_attach"),
)
_INDEXES = (
    ("uq_chat_attachment_live_address", "uq_channel_attachment_live_address"),
    ("idx_chat_attachment_conversation", "idx_channel_attachment_conversation"),
)


def upgrade() -> None:
    op.rename_table("chat_attachment", "channel_attachment")
    for old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE channel_attachment RENAME CONSTRAINT {old} TO {new}")
    for old, new in _INDEXES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")


def downgrade() -> None:
    for old, new in _INDEXES:
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    for old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE channel_attachment RENAME CONSTRAINT {new} TO {old}")
    op.rename_table("channel_attachment", "chat_attachment")
