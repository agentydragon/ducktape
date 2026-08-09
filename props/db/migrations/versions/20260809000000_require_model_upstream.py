"""Require every Props model to name an upstream.

Revision ID: 20260809000000
Revises: 20260619000000
"""

import sqlalchemy as sa
from alembic import op

revision = "20260809000000"
down_revision = "20260619000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM model_metadata WHERE upstream_name IS NULL")
    op.alter_column("model_metadata", "upstream_name", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    op.alter_column("model_metadata", "upstream_name", existing_type=op.f("sa.String"), nullable=True)
