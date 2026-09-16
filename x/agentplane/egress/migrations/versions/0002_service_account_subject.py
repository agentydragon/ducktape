"""Record the ServiceAccount subject of a decision, for requests no Sandbox owns."""

import sqlalchemy as sa
from alembic import op

revision = "0002_service_account_subject"
down_revision = "0001_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("egress_decision", sa.Column("service_account", sa.Text()))


def downgrade() -> None:
    op.drop_column("egress_decision", "service_account")
