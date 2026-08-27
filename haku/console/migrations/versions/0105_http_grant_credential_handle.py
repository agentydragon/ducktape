"""Name the Console-owned credential a temporary HTTP egress grant redeems.

A grant that redeems a credential names it by inert handle (#4885); the decide endpoint resolves
the handle against the deploy-config credential registry (`http_decide_config`) into a
per-request placeholder substitution. Expand-only: a nullable column, NULL keeping every existing
grant pure reachability. Credential values stay in deployment env references, never in Postgres.

Revision ID: 0105
Revises: 0103
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0105"
down_revision: str | None = "0103"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("http_grants", sa.Column("credential_handle", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_http_grants_credential_handle_nonempty",
        "http_grants",
        "credential_handle IS NULL OR btrim(credential_handle) <> ''",
    )


def downgrade() -> None:
    op.drop_constraint("ck_http_grants_credential_handle_nonempty", "http_grants")
    op.drop_column("http_grants", "credential_handle")
