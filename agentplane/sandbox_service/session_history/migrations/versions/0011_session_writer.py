"""Bind a Session to its ingress writer, and let a Session pushed through the ingress have no Sandbox.

A row is a Sandbox incarnation's (written by a follower) or a writer's (pushed through the ingress),
or both when an ingress push binds a row a follower created.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_session_writer"
down_revision = "0010_drop_registry_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("writer_namespace", "writer_name", "writer_uid"):
        op.add_column("session_history", sa.Column(name, sa.String(), nullable=True))
    op.alter_column("session_history", "sandbox_namespace", nullable=True)
    op.alter_column("session_history", "sandbox_name", nullable=True)
    op.create_check_constraint(
        "history_writer_complete", "session_history", "num_nulls(writer_namespace, writer_name, writer_uid) IN (0, 3)"
    )
    op.create_check_constraint(
        "history_sandbox_complete", "session_history", "num_nulls(sandbox_namespace, sandbox_name) IN (0, 2)"
    )
    op.create_check_constraint(
        "history_origin", "session_history", "sandbox_namespace IS NOT NULL OR writer_uid IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("history_origin", "session_history")
    op.drop_constraint("history_sandbox_complete", "session_history")
    op.drop_constraint("history_writer_complete", "session_history")
    op.alter_column("session_history", "sandbox_name", nullable=False)
    op.alter_column("session_history", "sandbox_namespace", nullable=False)
    for name in ("writer_uid", "writer_name", "writer_namespace"):
        op.drop_column("session_history", name)
