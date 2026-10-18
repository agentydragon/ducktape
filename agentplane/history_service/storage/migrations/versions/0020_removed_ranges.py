"""Record cursor ranges a cleanup deleted from a Session's stored history."""

import sqlalchemy as sa
from alembic import op

revision = "0020_removed_ranges"
down_revision = "0019_empty_admitted_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_event_removed_range",
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("session_history.id"), primary_key=True, nullable=False),
        sa.Column("first_cursor", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("last_cursor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("first_cursor <= last_cursor", name="removed_range_ordered"),
    )


def downgrade() -> None:
    op.drop_table("session_event_removed_range")
