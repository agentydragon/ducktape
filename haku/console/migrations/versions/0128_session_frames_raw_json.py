"""Store session frame payloads as raw JSON text.

Revision ID: 0128
Revises: 0127
"""

from __future__ import annotations

from alembic import op

revision: str = "0128"
down_revision: str | None = "0127"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE session_frames ALTER COLUMN payload TYPE json USING payload::text::json")


def downgrade() -> None:
    op.execute("ALTER TABLE session_frames ALTER COLUMN payload TYPE jsonb USING payload::jsonb")
