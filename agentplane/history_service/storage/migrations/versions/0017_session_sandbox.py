"""Record the Sandbox that wrote each Session.

Rows with a writer take it from the writer's ServiceAccount, which is named for its Sandbox. Rows
pushed before writers were bound (a locator the earlier follower recorded was dropped in
0014) stay empty until imported from the Runner Gateway's registry.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_session_sandbox"
down_revision = "0016_recovery_reason_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_history", sa.Column("sandbox_namespace", sa.String(), nullable=True))
    op.add_column("session_history", sa.Column("sandbox_name", sa.String(), nullable=True))
    op.create_check_constraint(
        "history_sandbox_complete", "session_history", "num_nulls(sandbox_namespace, sandbox_name) IN (0, 2)"
    )
    op.execute(
        "UPDATE session_history SET sandbox_namespace = writer_namespace, sandbox_name = writer_name "
        "WHERE writer_uid IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("history_sandbox_complete", "session_history")
    op.drop_column("session_history", "sandbox_name")
    op.drop_column("session_history", "sandbox_namespace")
