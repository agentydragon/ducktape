"""Drop the fold summary's resume marker: the app keeps it on its own `thread` row."""

import sqlalchemy as sa
from alembic import op

revision = "0012_drop_resumed_after_cursor"
down_revision = "0011_session_writer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("thread_history_summary", "resumed_after_cursor")


def downgrade() -> None:
    op.add_column("thread_history_summary", sa.Column("resumed_after_cursor", sa.BigInteger(), nullable=True))
