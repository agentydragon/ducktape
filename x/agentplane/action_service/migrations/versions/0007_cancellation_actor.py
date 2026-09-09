"""Retain authenticated cancellation attribution in the canonical transition log."""

import sqlalchemy as sa
from alembic import op

revision = "0007_cancellation_actor"
down_revision = "0006_decision_note"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_event", sa.Column("actor_principal", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("action_event", "actor_principal")
