"""Drop the `external_connection_binding_change` table, which nothing reads.

Dropping it discards every recorded rebind. Downgrade recreates the table empty.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_drop_binding_change"
down_revision = "0024_drop_grant_caller"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("external_connection_binding_change")


def downgrade() -> None:
    op.create_table(
        "external_connection_binding_change",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("external_connection.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_caller", postgresql.JSONB(), nullable=False),
        sa.Column("caller", postgresql.JSONB(), nullable=False),
        sa.Column("operator_issuer", sa.Text(), nullable=False),
        sa.Column("operator_subject", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
