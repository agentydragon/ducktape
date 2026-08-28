"""Merge the 0114/0115 migration fork into a single head.

#5067 (0114, `conversation.harness_kind`) and #5069 (0115, dissolving the stored
`kubernetes_grants.status`) both landed with `down_revision = "0112"`, forking the chain
into two heads — so `alembic upgrade head` errors with multiple heads. The two touch
independent tables and neither depends on the other, so this empty merge migration
rejoins them: `upgrade head` applies both branches (in either order) and resolves to the
single head 0116.

Revision ID: 0116
Revises: 0114, 0115
"""

from __future__ import annotations

revision: str = "0116"
down_revision: tuple[str, str] = ("0114", "0115")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
