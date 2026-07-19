"""Key external OAuth grants by logical operator connection.

The old schema allowed only one row per provider. Existing grants and pending flows are
intentionally discarded: a single broad Google grant cannot be assigned honestly to both of the
new independently scoped Gmail and Calendar connections.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A provider-keyed token has no unambiguous logical destination after one provider can back
    # multiple connections. Reconnecting produces correctly scoped, independently stored grants.
    op.execute("DELETE FROM provider_connection_flows")
    op.execute("DELETE FROM provider_connections")

    op.drop_constraint("provider_connections_pkey", "provider_connections", type_="primary")
    op.add_column("provider_connections", sa.Column("connection_name", sa.Text(), nullable=False))
    op.create_primary_key("provider_connections_pkey", "provider_connections", ["operator_id", "connection_name"])

    op.add_column("provider_connection_flows", sa.Column("connection_name", sa.Text(), nullable=False))


def downgrade() -> None:
    # Multiple logical rows for one provider cannot be collapsed deterministically.
    op.execute("DELETE FROM provider_connection_flows")
    op.execute("DELETE FROM provider_connections")

    op.drop_column("provider_connection_flows", "connection_name")
    op.drop_constraint("provider_connections_pkey", "provider_connections", type_="primary")
    op.drop_column("provider_connections", "connection_name")
    op.create_primary_key("provider_connections_pkey", "provider_connections", ["operator_id", "provider"])
