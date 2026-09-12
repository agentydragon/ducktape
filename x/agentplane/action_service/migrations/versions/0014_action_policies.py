"""ServiceAccount callers and policy evidence for the action-policy slice.

The caller a grant or consent acts as becomes a typed JSON ServiceAccount reference. Rows bound
to a configured Identity, the pre-ServiceAccount principal, are deleted rather than carried: no
grant, Connection, enrollment or externally submitted Action from before this migration survives.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_action_policies"
down_revision = "0013_mcp_server_linkage"
branch_labels = None
depends_on = None


def _drop_external_rows() -> None:
    op.execute("DELETE FROM action_request WHERE external_grant IS NOT NULL")
    op.execute("DELETE FROM connection_enrollment")
    op.execute("DELETE FROM external_connection_grant")
    op.execute("DELETE FROM external_connection")


def upgrade() -> None:
    _drop_external_rows()
    op.drop_column("external_connection_grant", "identity_id")
    op.add_column("external_connection_grant", sa.Column("caller", postgresql.JSONB(), nullable=False))
    op.drop_column("connection_enrollment", "identity_id")
    op.add_column("connection_enrollment", sa.Column("caller", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("action_decision", sa.Column("policy_evidence", postgresql.JSONB(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("action_decision", "policy_evidence")
    _drop_external_rows()
    op.drop_column("connection_enrollment", "caller")
    op.add_column("connection_enrollment", sa.Column("identity_id", sa.Text(), nullable=True))
    op.drop_column("external_connection_grant", "caller")
    op.add_column("external_connection_grant", sa.Column("identity_id", sa.Text(), nullable=False))
