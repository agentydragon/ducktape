"""Make a thread row's index unique within its scope, so a numbering bug fails the write rather than
giving two rows one position."""

from alembic import op

revision = "0012_unique_entity_index"
down_revision = "0011_event_log_identity"
branch_labels = None
depends_on = None

_INDEX = "ix_thread_entity_scope_entity_index"
_COLUMNS = ["thread_id", "projection_epoch", "entity_index"]


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="thread_entity")
    op.create_index(_INDEX, "thread_entity", _COLUMNS, unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="thread_entity")
    op.create_index(_INDEX, "thread_entity", _COLUMNS)
