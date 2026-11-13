"""Require every holding snapshot to belong to a complete capture."""

import sqlalchemy as sa
from alembic import op

revision = "0011_required_holding_capture"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("holding_snapshots", "capture_id", existing_type=sa.Integer(), nullable=False)
    op.create_table_comment("holding_snapshots", "Positions belonging to complete holdings captures.")


def downgrade() -> None:
    raise NotImplementedError("Holding snapshots require a capture; use a corrective forward migration")
