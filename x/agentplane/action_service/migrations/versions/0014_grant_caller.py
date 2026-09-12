"""Store the caller a grant or consent acts as, a ServiceAccount from now on, as a typed JSON value.

Existing rows keep their configured Identity under the same column as `{"identity_id": ...}`, so
the inventory and every Action's provenance stay readable; nothing resolves such a grant again.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_grant_caller"
down_revision = "0013_mcp_server_linkage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_connection_grant", sa.Column("caller", postgresql.JSONB(), nullable=True))
    op.execute("UPDATE external_connection_grant SET caller = jsonb_build_object('identity_id', identity_id)")
    op.alter_column("external_connection_grant", "caller", nullable=False)
    op.drop_column("external_connection_grant", "identity_id")
    op.add_column("connection_enrollment", sa.Column("caller", postgresql.JSONB(none_as_null=True), nullable=True))
    op.execute(
        "UPDATE connection_enrollment SET caller = jsonb_build_object('identity_id', identity_id) "
        "WHERE identity_id IS NOT NULL"
    )
    op.drop_column("connection_enrollment", "identity_id")


def downgrade() -> None:
    op.add_column("connection_enrollment", sa.Column("identity_id", sa.Text(), nullable=True))
    op.execute("UPDATE connection_enrollment SET identity_id = caller ->> 'identity_id' WHERE caller IS NOT NULL")
    op.drop_column("connection_enrollment", "caller")
    op.add_column("external_connection_grant", sa.Column("identity_id", sa.Text(), nullable=True))
    # A ServiceAccount grant has no configured Identity to fall back to; its name is the nearest.
    op.execute(
        "UPDATE external_connection_grant SET identity_id = coalesce(caller ->> 'identity_id', caller ->> 'name')"
    )
    op.alter_column("external_connection_grant", "identity_id", nullable=False)
    op.drop_column("external_connection_grant", "caller")
