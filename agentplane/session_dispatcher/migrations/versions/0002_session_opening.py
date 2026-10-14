"""Create the Session openings."""

import sqlalchemy as sa
from alembic import op

revision = "0002_session_opening"
down_revision = "0001_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_opening",
        sa.Column("session_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("sandbox_name", sa.String(), nullable=False),
        sa.Column("sandbox_uid", sa.Uuid(), nullable=False),
        sa.Column("spec", sa.LargeBinary(), nullable=False),
        sa.Column("setup_script", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("attached", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_opening_sandbox_uid", "session_opening", ["sandbox_uid"])
