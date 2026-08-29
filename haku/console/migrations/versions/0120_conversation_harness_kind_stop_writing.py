"""Stop writing the legacy conversation harness discriminator.

C4d contract release 2 of #4772. Release 0118 made ``harness_kind`` authoritative for reads and
required it to be populated. Once that image has converged, this release removes the ORM mapping
and dual-write, then makes the legacy column nullable so an old writer cannot fail a new row insert.
The physical column and its CHECK remain for the final drop release.

Revision ID: 0120
Revises: 0119
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column("conversation", "runtime_kind", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE conversation SET runtime_kind = harness_kind WHERE runtime_kind IS NULL")
    op.alter_column("conversation", "runtime_kind", existing_type=sa.Text(), nullable=False)
