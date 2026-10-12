"""Number Sessions in the order they are recorded, so a reader can follow the ones recorded after a position.

Rows already recorded take positions in no particular order.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_session_opened_position"
down_revision = "0014_drop_sandbox_locator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_history", sa.Column("opened_position", sa.BigInteger(), sa.Identity(), nullable=False))
    op.create_index("ux_history_opened_position", "session_history", ["opened_position"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_history_opened_position", "session_history")
    op.drop_column("session_history", "opened_position")
