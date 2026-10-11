"""Drop the `event_log` launch copies; a Thread's harness, model, cwd and model activity live on its
fold summary."""

import sqlalchemy as sa
from alembic import op

revision = "0029_event_log_copies_drop"
down_revision = "0028_event_log_copies_nullable"
branch_labels = None
depends_on = None

COPIES = {
    "harness": sa.String(length=14),
    "model": sa.Text(),
    "cwd": sa.Text(),
    "last_model_activity_at": sa.DateTime(timezone=True),
}


def upgrade() -> None:
    for column in COPIES:
        op.drop_column("event_log", column)


def downgrade() -> None:
    # The values are gone; the columns come back empty.
    for column, type_ in COPIES.items():
        op.add_column("event_log", sa.Column(column, type_, nullable=True))
