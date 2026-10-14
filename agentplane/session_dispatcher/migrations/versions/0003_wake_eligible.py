"""Persist authenticated wake eligibility and the incarnation a wake replaces."""

import sqlalchemy as sa
from alembic import op

revision = "0003_wake_eligible"
down_revision = "0002_session_opening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dispatch", sa.Column("wake_eligible", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("dispatch", sa.Column("wake_from_incarnation", sa.String(), nullable=True))
    op.add_column(
        "session_opening", sa.Column("wake_eligible", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("session_opening", sa.Column("wake_from_incarnation", sa.String(), nullable=True))
