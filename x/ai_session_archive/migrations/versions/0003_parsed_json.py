"""Store parsed transcript records in PostgreSQL JSON."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_parsed_json"
down_revision = "0002_codex_titles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "record_payloads",
        "parsed",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        postgresql_using="parsed::json",
    )


def downgrade() -> None:
    op.alter_column(
        "record_payloads",
        "parsed",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        postgresql_using="parsed::jsonb",
    )
