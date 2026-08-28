"""Dissolve the stored `kubernetes_grants.status`/`ended_at` into derived state (#4883 contract).

Contract half of the expand/contract begun in #5018/#4889: the end-fact columns
(`released_at`/`revoked_at`) and the envelope's `derive_status` already landed (0112), and this
image no longer reads or writes `status`/`ended_at`. A pre-facts replica during the roll could end
a grant by writing `status`/`ended_at` only, so first backfill those stragglers — `released`/
`revoked` carried `ended_at < expires_at`, so the fact derives the same terminal status — and null
the sweeper-written `end_reason` on expired rows, which record no fact. Then drop the stored
`status`/`ended_at`, the three status-bearing indexes, and the status-vocabulary CHECK, replacing
them with the envelope's fact-shape CHECK (`ck_kubernetes_grants_end_shape`, now shared by
`grant_envelope_table_args`) and expiry-shaped indexes matching `http_grants`. No rows are deleted
— grants are audit state outside the conversation-drop allowance.

Revision ID: 0115
Revises: 0112
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0115"
down_revision: str | None = "0112"
branch_labels: str | None = None
depends_on: str | None = None

# The envelope's fact-shape CHECK, identical to `grants.envelope.grant_envelope_table_args` and to
# `http_grants`: at most one end action, and a reason exactly when one is recorded.
_END_SHAPE = (
    "num_nonnulls(released_at, revoked_at) <= 1 "
    "AND ((num_nonnulls(released_at, revoked_at) = 1) = (end_reason IS NOT NULL)) "
    "AND (end_reason IS NULL OR btrim(end_reason) <> '')"
)

# derive_status expressed in SQL: an end action recorded before expiry wins; otherwise the clock
# decides active vs expired. Shared by the downgrade's status/ended_at reconstruction.
_DERIVED_STATUS = (
    "CASE "
    "WHEN released_at IS NOT NULL AND released_at < expires_at THEN 'released' "
    "WHEN revoked_at IS NOT NULL AND revoked_at < expires_at THEN 'revoked' "
    "WHEN expires_at <= statement_timestamp() THEN 'expired' "
    "ELSE 'active' END"
)


def upgrade() -> None:
    op.execute("UPDATE kubernetes_grants SET released_at = ended_at WHERE status = 'released' AND released_at IS NULL")
    op.execute("UPDATE kubernetes_grants SET revoked_at = ended_at WHERE status = 'revoked' AND revoked_at IS NULL")
    op.execute("UPDATE kubernetes_grants SET end_reason = NULL WHERE status = 'expired'")

    op.drop_index("idx_kubernetes_grants_owner_status_expiry", table_name="kubernetes_grants")
    op.drop_index("idx_kubernetes_grants_agent_principal_status_expiry", table_name="kubernetes_grants")
    op.drop_index("idx_kubernetes_grants_session_principal_status_expiry", table_name="kubernetes_grants")
    op.drop_constraint("ck_kubernetes_grants_status_shape", "kubernetes_grants", type_="check")
    op.drop_constraint("ck_kubernetes_grants_single_end_action", "kubernetes_grants", type_="check")
    op.create_check_constraint("ck_kubernetes_grants_end_shape", "kubernetes_grants", _END_SHAPE)
    op.drop_column("kubernetes_grants", "status")
    op.drop_column("kubernetes_grants", "ended_at")
    op.create_index("idx_kubernetes_grants_owner_expiry", "kubernetes_grants", ["owner_agent_id", "expires_at"])
    op.create_index(
        "idx_kubernetes_grants_agent_principal_expiry", "kubernetes_grants", ["principal_agent_id", "expires_at"]
    )
    op.create_index(
        "idx_kubernetes_grants_session_principal_expiry", "kubernetes_grants", ["principal_session_id", "expires_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_kubernetes_grants_session_principal_expiry", table_name="kubernetes_grants")
    op.drop_index("idx_kubernetes_grants_agent_principal_expiry", table_name="kubernetes_grants")
    op.drop_index("idx_kubernetes_grants_owner_expiry", table_name="kubernetes_grants")
    op.add_column("kubernetes_grants", sa.Column("status", sa.Text(), nullable=True))
    op.add_column("kubernetes_grants", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    # Reconstruct the stored pair from the facts: a terminal end fact supplies ended_at and keeps
    # its end_reason; a derived expiry supplies expires_at and the sweeper's 'expired' reason.
    op.execute(
        sa.text(
            f"""
            UPDATE kubernetes_grants SET
                status = {_DERIVED_STATUS},
                ended_at = CASE
                    WHEN released_at IS NOT NULL AND released_at < expires_at THEN released_at
                    WHEN revoked_at IS NOT NULL AND revoked_at < expires_at THEN revoked_at
                    WHEN expires_at <= statement_timestamp() THEN expires_at
                    ELSE NULL
                END,
                end_reason = CASE
                    WHEN released_at IS NOT NULL AND released_at < expires_at THEN end_reason
                    WHEN revoked_at IS NOT NULL AND revoked_at < expires_at THEN end_reason
                    WHEN expires_at <= statement_timestamp() THEN 'expired'
                    ELSE NULL
                END
            """
        )
    )
    op.alter_column("kubernetes_grants", "status", nullable=False)
    op.drop_constraint("ck_kubernetes_grants_end_shape", "kubernetes_grants", type_="check")
    op.create_check_constraint(
        "ck_kubernetes_grants_status_shape",
        "kubernetes_grants",
        "(status = 'active' AND ended_at IS NULL AND end_reason IS NULL) OR "
        "(status IN ('released', 'revoked', 'expired') AND ended_at IS NOT NULL "
        "AND end_reason IS NOT NULL AND btrim(end_reason) <> '')",
    )
    op.create_check_constraint(
        "ck_kubernetes_grants_single_end_action", "kubernetes_grants", "num_nonnulls(released_at, revoked_at) <= 1"
    )
    op.create_index(
        "idx_kubernetes_grants_owner_status_expiry", "kubernetes_grants", ["owner_agent_id", "status", "expires_at"]
    )
    op.create_index(
        "idx_kubernetes_grants_agent_principal_status_expiry",
        "kubernetes_grants",
        ["principal_agent_id", "status", "expires_at"],
    )
    op.create_index(
        "idx_kubernetes_grants_session_principal_status_expiry",
        "kubernetes_grants",
        ["principal_session_id", "status", "expires_at"],
    )
