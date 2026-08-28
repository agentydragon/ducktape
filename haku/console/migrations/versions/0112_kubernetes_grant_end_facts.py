"""Give Kubernetes grants the envelope's end-fact columns (#4883 expand step, on #4889).

Expand-only: `kubernetes_grants` gains nullable ``released_at``/``revoked_at`` mirroring
`http_grants`, backfilled from the stored ``status``/``ended_at`` pair they will eventually
replace. Expired rows deliberately get no fact — expiry derives from ``expires_at`` and the
clock, matching the HTTP domain. The stored ``status`` and ``ended_at`` stay, dual-written by
the new image and still written by pre-facts replicas during the roll; the contract step that
flips readers onto the facts and drops them is staged at `KubernetesGrantRow`.

Revision ID: 0112
Revises: 0111
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0112"
down_revision: str | None = "0111"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("kubernetes_grants", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("kubernetes_grants", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE kubernetes_grants SET released_at = ended_at WHERE status = 'released'")
    op.execute("UPDATE kubernetes_grants SET revoked_at = ended_at WHERE status = 'revoked'")
    op.create_check_constraint(
        "ck_kubernetes_grants_single_end_action", "kubernetes_grants", "num_nonnulls(released_at, revoked_at) <= 1"
    )


def downgrade() -> None:
    op.drop_constraint("ck_kubernetes_grants_single_end_action", "kubernetes_grants")
    op.drop_column("kubernetes_grants", "revoked_at")
    op.drop_column("kubernetes_grants", "released_at")
