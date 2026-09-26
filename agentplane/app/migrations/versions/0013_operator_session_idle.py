"""Give an operator session an absolute deadline beside its expiry, which activity now moves forward.

Existing sessions are dropped rather than given a deadline: their payload predates the login tokens'
current shape, so their operators log in again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_operator_session_idle"
down_revision = "0012_unique_entity_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM operator_browser_session")
    op.add_column(
        "operator_browser_session", sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False)
    )


def downgrade() -> None:
    op.execute("DELETE FROM operator_browser_session")
    op.drop_column("operator_browser_session", "absolute_expires_at")
