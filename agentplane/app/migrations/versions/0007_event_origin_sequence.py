"""Drop the event origin sequence, which replication admits only when it equals the cursor."""

import sqlalchemy as sa
from alembic import op

revision = "0007_event_origin_sequence"
down_revision = "0006_thread_fold_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Indexed `(thread_id, origin_source_id, origin_sequence)`, which is the primary key with a
    # thread-constant column wedged in the middle.
    op.drop_index("ix_event_thread_origin", table_name="event")
    op.drop_column("event", "origin_sequence")


def downgrade() -> None:
    op.add_column("event", sa.Column("origin_sequence", sa.BigInteger(), nullable=True))
    op.execute('UPDATE event SET origin_sequence = "cursor"')
    op.alter_column("event", "origin_sequence", nullable=False)
    op.create_index("ix_event_thread_origin", "event", ["thread_id", "origin_source_id", "origin_sequence"])
