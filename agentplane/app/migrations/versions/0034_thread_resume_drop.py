"""Drop `thread.resumed_after_cursor`: the History Service's fold reopens a resumed feed itself."""

import sqlalchemy as sa
from alembic import op

revision = "0034_thread_resume_drop"
down_revision = "0033_event_log_sandbox_drop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("thread", "resumed_after_cursor")


def downgrade() -> None:
    # The values are gone; the column comes back empty.
    op.add_column("thread", sa.Column("resumed_after_cursor", sa.BigInteger(), nullable=True))
