"""Carry the caller-authored plaintext context an operator decides on."""

import sqlalchemy as sa
from alembic import op

revision = "0015_action_request_context"
down_revision = "0014_action_policies"
branch_labels = None
depends_on = None

# Requests submitted before the caller could supply a title still have to render as something on
# the approval surfaces; they carry this instead of an empty line.
BACKFILL_TITLE = "(untitled)"


def upgrade() -> None:
    op.add_column("action_request", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("action_request", sa.Column("description", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE action_request SET title = :title WHERE title IS NULL").bindparams(title=BACKFILL_TITLE))
    op.alter_column("action_request", "title", nullable=False)


def downgrade() -> None:
    op.drop_column("action_request", "description")
    op.drop_column("action_request", "title")
