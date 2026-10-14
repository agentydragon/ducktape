"""Record when a budgeted Sandbox wake was requested."""

import sqlalchemy as sa
from alembic import op

revision = "0006_wake_deadline"
down_revision = "0005_wake_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dispatch", "session_opening"):
        op.add_column(table, sa.Column("wake_requested_at", sa.DateTime(timezone=True), nullable=True))
