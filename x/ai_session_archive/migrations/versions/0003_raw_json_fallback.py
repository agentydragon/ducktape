"""Mark JSON-valid records whose values cannot be represented by PostgreSQL JSONB."""

import sqlalchemy as sa
from alembic import op

revision = "0003_raw_json_fallback"
down_revision = "0002_codex_titles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "record_payloads", sa.Column("parsed_from_raw", sa.Boolean(), server_default=sa.false(), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("record_payloads", "parsed_from_raw")
