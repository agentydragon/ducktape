"""Drop the grant's `caller` column, unread and unwritten since `0023_connection_binding_required`.

The Connection's `bound_caller` carries the current binding and the grant's `original_caller` its
history. Downgrade re-adds the column nullable, as `0023` left it, filled from `original_caller`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_drop_grant_caller"
down_revision = "0023_connection_binding_required"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("external_connection_grant", "caller")


def downgrade() -> None:
    op.add_column("external_connection_grant", sa.Column("caller", postgresql.JSONB(), nullable=True))
    op.execute("UPDATE external_connection_grant SET caller = original_caller")
