"""Use the runner protocol Harness vocabulary for persisted Threads.

This is a hard cutover: previously persisted thread projections and their dependent feed/event
rows are discarded rather than translating an obsolete agent-identity representation.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_thread_harness"
down_revision = "0002_thread_archived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("TRUNCATE thread CASCADE")
    op.alter_column(
        "thread",
        "provider",
        new_column_name="harness",
        existing_type=sa.Text(),
        type_=sa.String(length=14),
        existing_nullable=False,
    )


def downgrade() -> None:
    raise RuntimeError("0003_thread_harness is a hard data-model cutover and cannot be downgraded safely")
