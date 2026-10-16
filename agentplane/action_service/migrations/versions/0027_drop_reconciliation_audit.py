"""Drop unused reconciliation attribution after all readers and writers have rolled out."""

import sqlalchemy as sa
from alembic import op

revision = "0027_drop_reconciliation_audit"
down_revision = "0026_drop_retired_policy_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("action_execution", "reconciliation_source")
    op.drop_column("action_execution", "reconciled_by")


def downgrade() -> None:
    # The retired attribution values cannot be recovered.
    op.add_column("action_execution", sa.Column("reconciliation_source", sa.Text()))
    op.add_column("action_execution", sa.Column("reconciled_by", sa.Text()))
