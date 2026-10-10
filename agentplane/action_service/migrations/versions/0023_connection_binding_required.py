"""Backfill the binding columns rows written before `0021_connection_rebind` could leave empty.

Every grant gets its `original_caller`, which becomes NOT NULL. A Connection without
`bound_caller` that was never rebound takes its unrevoked grant's caller, as readers used to
derive it; an unbound Connection, whose grants are all revoked, stays NULL.
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


def downgrade() -> None:
    op.alter_column("external_connection_grant", "original_caller", existing_type=postgresql.JSONB(), nullable=True)
