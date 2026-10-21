"""Drop the Connection grant recorded on each request after every reader and writer has rolled out."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_drop_external_grant"
down_revision = "0027_drop_reconciliation_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("action_request", "external_grant")


def downgrade() -> None:
    # The dropped grants cannot be recovered.
    op.add_column("action_request", sa.Column("external_grant", postgresql.JSONB()))
