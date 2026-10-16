"""Drop the retired opening attachment snapshot."""

import sqlalchemy as sa
from alembic import op

revision = "0007_drop_opening_attached"
down_revision = "0006_wake_deadline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("session_opening", "attached")


def downgrade() -> None:
    op.add_column("session_opening", sa.Column("attached", sa.LargeBinary(), nullable=True))
