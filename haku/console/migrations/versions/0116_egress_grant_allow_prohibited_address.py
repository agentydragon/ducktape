"""Merge the 0112 migration fork and add ``http_grants.allow_prohibited_address``.

Two children of 0112 both reached devel — 0114 (#5067) and 0115 (#5069) — leaving alembic with two
heads. This revision merges them so ``upgrade head`` is single-headed again, and in the same step
adds the egress-grant capability column.

``allow_prohibited_address`` is the reusable, destination-scoped primitive letting a temporary HTTP
grant reach one exact origin that resolves entirely into otherwise-prohibited (cluster-internal)
address space. The decide oracle (``haku/console/grants/http/decide_service.py``) owns the address
check and the scoping; the column only records which grants carry the capability. ``NOT NULL DEFAULT
false``, so every existing grant — and any inserted without the column during a roll — stays
default-deny.

Revision ID: 0116
Revises: 0114, 0115
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0116"
down_revision: tuple[str, str] = ("0114", "0115")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "http_grants",
        sa.Column("allow_prohibited_address", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("http_grants", "allow_prohibited_address")
