"""Store a Thread's runner source once, on its checkpoint.

`TrajectoryStore.record` admits one `origin.source_id` per Thread and `thread_checkpoint` is keyed
by `thread_id` alone, so the column repeated across five composite keys, the stored payload
references and the archive partitioned nothing. The fold's scope is `(thread_id, projection_epoch)`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_thread_scope_source_id"
down_revision = "0007_event_origin_sequence"
branch_labels = None
depends_on = None

# Each fold table's primary key after `thread_id`, without the source.
KEYS = {
    "thread_entity": ("projection_epoch", "entity_kind", "entity_id"),
    "thread_payload_manifest": (
        "projection_epoch",
        "owner_cursor",
        "owner_id",
        "field",
        "generation",
        "revision_cursor",
    ),
    "thread_payload_chunk": ("projection_epoch", "owner_cursor", "owner_id", "field", "generation", "chunk_index"),
    "thread_evidence": ("projection_epoch", "entity_cursor", "observation_cursor"),
    "thread_native_link": ("projection_epoch", "entity_cursor", "observation_cursor", "source_sequence"),
}
# Likewise, each scope index's columns after the scope itself.
ENTITY_INDEXES = {
    "ix_thread_entity_scope_revision": (("revision_cursor",), None),
    "ix_thread_entity_scope_cursor": (("cursor", "entity_kind", "entity_id"), None),
    "ix_thread_entity_scope_pending_cursor": (("cursor", "entity_kind", "entity_id"), "pending"),
    "ix_thread_entity_scope_segment_cursor": (("cursor",), "entity_kind IN ('item', 'confirmed_input', 'lifecycle')"),
}
REFERENCE_COLUMNS = ("text_ref", "arguments_ref", "output_ref", "input_ref")


def _rekey(table: str, key: tuple[str, ...]) -> None:
    op.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{table}_pkey" PRIMARY KEY ({", ".join(key)})')


def _drop_keys() -> None:
    for table in KEYS:
        op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{table}_pkey"')


def _entity_indexes(scope: tuple[str, ...]) -> None:
    for name, (columns, where) in ENTITY_INDEXES.items():
        op.create_index(
            name, "thread_entity", [*scope, *columns], postgresql_where=sa.text(where) if where is not None else None
        )


def upgrade() -> None:
    for name in ENTITY_INDEXES:
        op.drop_index(name, table_name="thread_entity")
    # Dropping a key column would take the whole constraint with it, so rebuild each explicitly.
    _drop_keys()
    for table, key in KEYS.items():
        op.drop_column(table, "source_id")
        _rekey(table, ("thread_id", *key))
    _entity_indexes(("thread_id", "projection_epoch"))
    for column in REFERENCE_COLUMNS:
        op.execute(f"UPDATE thread_entity SET {column} = {column} - 'source_id' WHERE {column} IS NOT NULL")
    # The archive names its source in every row's own proto-JSON payload; the column repeated it.
    op.drop_column("event", "origin_source_id")


def downgrade() -> None:
    # `0007`'s downgrade rebuilds `ix_event_thread_origin` over this column, so restore it first.
    op.add_column("event", sa.Column("origin_source_id", sa.Text(), nullable=True))
    op.execute("UPDATE event SET origin_source_id = payload -> 'origin' ->> 'sourceId'")
    op.alter_column("event", "origin_source_id", nullable=False)
    for name in ENTITY_INDEXES:
        op.drop_index(name, table_name="thread_entity")
    _drop_keys()
    for table, key in KEYS.items():
        op.add_column(table, sa.Column("source_id", sa.Text(), nullable=True))
        op.execute(
            f"UPDATE {table} SET source_id = thread_checkpoint.source_id "
            f"FROM thread_checkpoint WHERE {table}.thread_id = thread_checkpoint.thread_id"
        )
        op.alter_column(table, "source_id", nullable=False)
        _rekey(table, ("thread_id", "source_id", *key))
    _entity_indexes(("thread_id", "source_id", "projection_epoch"))
    for column in REFERENCE_COLUMNS:
        op.execute(
            f"UPDATE thread_entity SET {column} = "
            f"jsonb_build_object('source_id', source_id) || {column} "
            f"WHERE {column} IS NOT NULL"
        )
