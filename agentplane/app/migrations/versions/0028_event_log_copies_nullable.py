"""Let `event_log` rows omit the harness, model and cwd copies the app no longer reads.

A replica on the previous image still writes them during a roll; the columns drop in a later
migration, once no replica runs an image that writes them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_event_log_copies_nullable"
down_revision = "0027_summary_first_event"
branch_labels = None
depends_on = None

COPIES = {"harness": sa.String(length=14), "model": sa.Text(), "cwd": sa.Text()}


def upgrade() -> None:
    for column, type_ in COPIES.items():
        op.alter_column("event_log", column, existing_type=type_, nullable=True)


def downgrade() -> None:
    # Fails while a row written by this image lacks a copy; there is nothing to restore it from.
    for column, type_ in COPIES.items():
        op.alter_column("event_log", column, existing_type=type_, nullable=False)
