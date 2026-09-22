"""Number a thread's rows densely, backfilling the order the fold would have assigned."""

import sqlalchemy as sa
from alembic import op

revision = "0009_thread_entity_index"
down_revision = "0008_thread_scope_source_id"
branch_labels = None
depends_on = None

# The order `_ordered_entity_rows` numbers a batch in, so a backfilled row gets the index
# it would have been given had the column always existed.
_BACKFILL = """
UPDATE thread_entity AS entity
SET entity_index = numbered.position
FROM (
    SELECT
        thread_id, projection_epoch, entity_kind, entity_id,
        row_number() OVER (
            PARTITION BY thread_id, projection_epoch
            ORDER BY cursor, entity_kind, entity_id
        ) - 1 AS position
    FROM thread_entity
) AS numbered
WHERE entity.thread_id = numbered.thread_id
  AND entity.projection_epoch = numbered.projection_epoch
  AND entity.entity_kind = numbered.entity_kind
  AND entity.entity_id = numbered.entity_id
"""


def upgrade() -> None:
    op.add_column("thread_entity", sa.Column("entity_index", sa.BigInteger(), nullable=True))
    op.execute(_BACKFILL)
    op.alter_column("thread_entity", "entity_index", nullable=False)
    op.create_index(
        "ix_thread_entity_scope_entity_index", "thread_entity", ["thread_id", "projection_epoch", "entity_index"]
    )


def downgrade() -> None:
    op.drop_index("ix_thread_entity_scope_entity_index", table_name="thread_entity")
    op.drop_column("thread_entity", "entity_index")
