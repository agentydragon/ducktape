"""Backfill the binding columns rows written before `0021_connection_rebind` could leave empty.

Every grant gets its `original_caller`, which becomes NOT NULL. A Connection without
`bound_caller` that was never rebound takes its unrevoked grant's caller, as readers used to
derive it; an unbound Connection, whose grants are all revoked, stays NULL.

The grant's `caller` column is left unread and unwritten: it becomes nullable here, so replicas
that no longer map it can insert grants, and a later migration drops it.
"""

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_connection_binding_required"
down_revision = "0022_connection_binding_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE external_connection_grant SET original_caller = caller WHERE original_caller IS NULL")
    op.alter_column("external_connection_grant", "original_caller", existing_type=postgresql.JSONB(), nullable=False)
    op.execute("""UPDATE external_connection AS c SET bound_caller = (
        SELECT g.caller FROM external_connection_grant AS g
        WHERE g.connection_id = c.id AND g.status <> 'revoked'
        ORDER BY g.revision DESC LIMIT 1
    ) WHERE c.bound_caller IS NULL AND c.binding_version = 0""")
    op.alter_column("external_connection_grant", "caller", existing_type=postgresql.JSONB(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE external_connection_grant SET caller = original_caller WHERE caller IS NULL")
    op.alter_column("external_connection_grant", "caller", existing_type=postgresql.JSONB(), nullable=False)
    op.alter_column("external_connection_grant", "original_caller", existing_type=postgresql.JSONB(), nullable=True)
