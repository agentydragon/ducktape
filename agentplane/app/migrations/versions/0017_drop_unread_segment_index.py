"""Drop the partial cursor index over the rendered kinds: the window is read by `entity_index` now, and no
query orders or filters thread rows by cursor within those kinds."""

import sqlalchemy as sa
from alembic import op

revision = "0017_drop_unread_segment_index"
down_revision = "0016_thread_payload_chunk_json"
branch_labels = None
depends_on = None

_INDEX = "ix_thread_entity_scope_segment_cursor"


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="thread_entity")


def downgrade() -> None:
    op.create_index(
        _INDEX,
        "thread_entity",
        ["thread_id", "projection_epoch", "cursor"],
        postgresql_where=sa.text("entity_kind IN ('item', 'confirmed_input', 'lifecycle')"),
    )
