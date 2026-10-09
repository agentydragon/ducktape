"""Name the binding-change audit for its data rather than its write operation.

0021 is already deployed: retain its history and rename the table in place.
"""

from alembic import op

revision = "0022_connection_binding_change"
down_revision = "0021_connection_rebind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("external_connection_rebind", "external_connection_binding_change")


def downgrade() -> None:
    op.rename_table("external_connection_binding_change", "external_connection_rebind")
