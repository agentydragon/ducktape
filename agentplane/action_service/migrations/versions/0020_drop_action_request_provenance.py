"""Drop unused caller-supplied Action Request provenance bags."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_drop_request_provenance"
down_revision = "0019_action_history_page"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("action_request", "correlation")
    op.drop_column("action_request", "origin")


def downgrade() -> None:
    # The values are intentionally discarded by upgrade; a downgrade restores empty bags.
    for column in ("origin", "correlation"):
        op.add_column(
            "action_request",
            sa.Column(column, postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        )
        op.alter_column("action_request", column, server_default=None)
