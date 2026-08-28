"""Add ``http_grants.allow_prohibited_address``.

``allow_prohibited_address`` is the reusable, destination-scoped primitive letting a temporary HTTP
grant reach one exact origin that resolves entirely into otherwise-prohibited (cluster-internal)
address space. The decide oracle (``haku/console/grants/http/decide_service.py``) owns the address
check and the scoping; the column only records which grants carry the capability. ``NOT NULL DEFAULT
false``, so every existing grant — and any inserted without the column during a roll — stays
default-deny.

Revision ID: 0117
Revises: 0116
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0117"
down_revision: str | None = "0116"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "http_grants",
        sa.Column("allow_prohibited_address", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("http_grants", "allow_prohibited_address")
