"""Drop `event_log.sandbox`: the Runner Gateway answers where each Session runs."""

import sqlalchemy as sa
from alembic import op

revision = "0033_event_log_sandbox_drop"
down_revision = "0032_app_projection_drop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("event_log", "sandbox")


def downgrade() -> None:
    # The values are gone; the column comes back empty.
    op.add_column("event_log", sa.Column("sandbox", sa.Text(), nullable=True))
