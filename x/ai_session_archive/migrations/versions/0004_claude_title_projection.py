"""Track Claude title projection versions for ingestion backfills."""

import sqlalchemy as sa
from alembic import op

revision = "0004_claude_title_projection"
down_revision = "0003_session_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("claude_title_projection_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "claude_title_projection_version")
