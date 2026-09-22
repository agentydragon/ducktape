"""Rename the materialized fold's tables from `conversation_*` to `thread_*`.

A Thread is the conversation; the projection's own tables needed no second noun for it.
Renames carry indexes, constraints, grants, replica identity and publication membership
with them, so the Electric publication keeps its existing members under the new names.
A stored payload reference names its owner `owner_id`, the same as the manifest and chunk
column it selects, rather than `owner_item_id` for an owner that is often not an item.
"""

from alembic import op

revision = "0006_thread_fold_rename"
down_revision = "0005_conversation_projection"
branch_labels = None
depends_on = None

TABLES = {
    "conversation_projection_checkpoint": "thread_checkpoint",
    "conversation_entity": "thread_entity",
    "conversation_payload_manifest": "thread_payload_manifest",
    "conversation_payload_chunk": "thread_payload_chunk",
    "conversation_projection_evidence": "thread_evidence",
    "conversation_projection_native_link": "thread_native_link",
}
INDEXES = {
    f"ix_conversation_entity_scope_{suffix}": f"ix_thread_entity_scope_{suffix}"
    for suffix in ("revision", "cursor", "pending_cursor", "segment_cursor")
}
REFERENCE_COLUMNS = ("text_ref", "arguments_ref", "output_ref", "input_ref")


def _rename(tables: dict[str, str], indexes: dict[str, str]) -> None:
    for old, new in tables.items():
        op.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')
        # A rename leaves the constraints under names derived from the old table.
        op.execute(f'ALTER TABLE "{new}" RENAME CONSTRAINT "{old}_pkey" TO "{new}_pkey"')
        op.execute(f'ALTER TABLE "{new}" RENAME CONSTRAINT "{old}_thread_id_fkey" TO "{new}_thread_id_fkey"')
    for old, new in indexes.items():
        op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')


def _rekey_references(old: str, new: str) -> None:
    for column in REFERENCE_COLUMNS:
        op.execute(
            f"UPDATE thread_entity SET {column} = "
            f"({column} - '{old}') || jsonb_build_object('{new}', {column} -> '{old}') "
            f"WHERE jsonb_exists({column}, '{old}')"
        )


def upgrade() -> None:
    _rename(TABLES, INDEXES)
    _rekey_references("owner_item_id", "owner_id")


def downgrade() -> None:
    _rekey_references("owner_id", "owner_item_id")
    _rename({new: old for old, new in TABLES.items()}, {new: old for old, new in INDEXES.items()})
