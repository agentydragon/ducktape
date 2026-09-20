"""Replace the unused string identity with a structured group/name value."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_structured_action"
down_revision = "0004_drop_action_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("action_request", "capability")
    op.add_column("action_request", sa.Column("action", postgresql.JSONB(), nullable=False))


def downgrade() -> None:
    raise NotImplementedError("There is no legacy Action identity representation")
