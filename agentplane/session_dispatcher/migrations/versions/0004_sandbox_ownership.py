"""Add the database fence for Sandbox delivery ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0004_sandbox_ownership"
down_revision = "0003_wake_eligible"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandbox_ownership",
        sa.Column("sandbox_uid", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_uid", sa.String(), nullable=False),
        sa.Column("lease_transitions", sa.Integer(), nullable=False),
        sa.Column("holder_identity", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
