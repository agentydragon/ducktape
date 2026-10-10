"""Pending submissions, which admission reconciliation polls for."""

import sqlalchemy as sa
from alembic import op

revision = "0005_pending_submission_index"
down_revision = "0004_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_command_submission_pending",
        "command_submission",
        ["session_id"],
        postgresql_where=sa.text("state = 'pending_admission'"),
    )


def downgrade() -> None:
    op.drop_index("ix_command_submission_pending", table_name="command_submission")
