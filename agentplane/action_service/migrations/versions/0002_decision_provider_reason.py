"""Add bounded provider-authored reason evidence to action_decision."""

import sqlalchemy as sa
from alembic import op

revision = "0002_decision_provider_reason"
down_revision = "0001_action_service"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_decision", sa.Column("reason_code", sa.Text()))
    op.add_column("action_decision", sa.Column("reason_description", sa.Text()))


def downgrade() -> None:
    op.drop_column("action_decision", "reason_description")
    op.drop_column("action_decision", "reason_code")
