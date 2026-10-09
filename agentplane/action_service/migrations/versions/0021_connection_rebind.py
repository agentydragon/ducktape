"""Retain original grant history and synchronize legacy readers on audited rebinding."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_connection_rebind"
down_revision = "0020_drop_request_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_connection_grant", sa.Column("original_caller", postgresql.JSONB(), nullable=True))
    op.execute("UPDATE external_connection_grant SET original_caller = caller")
    op.add_column("external_connection", sa.Column("bound_caller", postgresql.JSONB(), nullable=True))
    op.add_column("external_connection", sa.Column("binding_version", sa.Integer(), server_default="0", nullable=False))
    op.execute("""UPDATE external_connection AS c SET bound_caller = (
        SELECT g.caller FROM external_connection_grant AS g
        WHERE g.connection_id = c.id AND g.status <> 'revoked'
        ORDER BY g.revision DESC LIMIT 1
    )""")
    op.create_table(
        "external_connection_rebind",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("external_connection.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_caller", postgresql.JSONB(), nullable=False),
        sa.Column("caller", postgresql.JSONB(), nullable=False),
        sa.Column("operator_issuer", sa.Text(), nullable=False),
        sa.Column("operator_subject", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM external_connection_rebind)")).scalar_one():
        raise RuntimeError(
            "Cannot downgrade after a Connection rebind; restoring the old caller would change token authority"
        )
    op.drop_table("external_connection_rebind")
    op.execute("UPDATE external_connection_grant SET caller = COALESCE(original_caller, caller)")
    op.drop_column("external_connection_grant", "original_caller")
    op.drop_column("external_connection", "binding_version")
    op.drop_column("external_connection", "bound_caller")
