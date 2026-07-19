"""Record the deploy-named provider for each logical operator connection.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_connections", sa.Column("provider_name", sa.Text(), nullable=False))
    op.create_check_constraint(
        "ck_provider_connections_connection_name_nonempty", "provider_connections", "btrim(connection_name) <> ''"
    )
    op.create_check_constraint(
        "ck_provider_connections_provider_name_nonempty", "provider_connections", "btrim(provider_name) <> ''"
    )

    op.add_column("provider_connection_flows", sa.Column("provider_name", sa.Text(), nullable=False))
    op.create_check_constraint(
        "ck_provider_connection_flows_connection_name_nonempty",
        "provider_connection_flows",
        "btrim(connection_name) <> ''",
    )
    op.create_check_constraint(
        "ck_provider_connection_flows_provider_name_nonempty", "provider_connection_flows", "btrim(provider_name) <> ''"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_provider_connection_flows_provider_name_nonempty", "provider_connection_flows", type_="check"
    )
    op.drop_constraint(
        "ck_provider_connection_flows_connection_name_nonempty", "provider_connection_flows", type_="check"
    )
    op.drop_column("provider_connection_flows", "provider_name")

    op.drop_constraint("ck_provider_connections_provider_name_nonempty", "provider_connections", type_="check")
    op.drop_constraint("ck_provider_connections_connection_name_nonempty", "provider_connections", type_="check")
    op.drop_column("provider_connections", "provider_name")
