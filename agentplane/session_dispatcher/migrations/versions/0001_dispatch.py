"""Create the dispatch queue."""

import sqlalchemy as sa
from alembic import op

revision = "0001_dispatch"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dispatch",
        sa.Column("position", sa.BigInteger(), sa.Identity(), primary_key=True, nullable=False),
        sa.Column("command_id", sa.String(), nullable=False, unique=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("sandbox_name", sa.String(), nullable=False),
        sa.Column("sandbox_uid", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("command", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("admission", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dispatch_session_state", "dispatch", ["session_id", "state"])
    op.create_index("ix_dispatch_sandbox_uid_state", "dispatch", ["sandbox_uid", "state"])
    op.create_index("ix_dispatch_sandbox_name", "dispatch", ["sandbox_name"])
