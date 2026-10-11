"""Sessions are created by the runner's own retry check, so the registry keeps no Open reservation."""

import sqlalchemy as sa
from alembic import op

revision = "0009_session_open_columns"
down_revision = "0008_dial_out_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ux_session_open_key", table_name="session")
    for column in ("caller_namespace", "caller_name", "open_key", "open_request", "launch_spec"):
        op.drop_column("session", column)


def downgrade() -> None:
    # Empty: a downgraded gateway reads no reservation as no Open in flight.
    op.add_column("session", sa.Column("caller_namespace", sa.String(), nullable=True))
    op.add_column("session", sa.Column("caller_name", sa.String(), nullable=True))
    op.add_column("session", sa.Column("open_key", sa.String(), nullable=True))
    op.add_column("session", sa.Column("open_request", sa.LargeBinary(), nullable=True))
    op.add_column("session", sa.Column("launch_spec", sa.LargeBinary(), nullable=True))
    op.create_index(
        "ux_session_open_key",
        "session",
        ["caller_namespace", "caller_name", "sandbox_namespace", "sandbox_name", "sandbox_uid", "open_key"],
        unique=True,
        postgresql_where=sa.text("open_key IS NOT NULL"),
    )
