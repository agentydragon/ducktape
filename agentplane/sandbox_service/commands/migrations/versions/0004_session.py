"""The Sandbox Service's Session registry, copied from the history database at startup."""

import sqlalchemy as sa
from alembic import op

revision = "0004_session"
down_revision = "0003_submission_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("session_change_position")))
    op.create_table(
        "session",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("sandbox_namespace", sa.String(), nullable=False),
        sa.Column("sandbox_name", sa.String(), nullable=False),
        sa.Column("sandbox_uid", sa.Uuid(), nullable=True),
        sa.Column("runner_session_id", sa.String(), nullable=True),
        sa.Column("caller_namespace", sa.String(), nullable=True),
        sa.Column("caller_name", sa.String(), nullable=True),
        sa.Column("open_key", sa.String(), nullable=True),
        sa.Column("open_request", sa.LargeBinary(), nullable=True),
        sa.Column("launch_spec", sa.LargeBinary(), nullable=True),
        sa.Column("change_position", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ux_session_runner_locator",
        "session",
        ["sandbox_namespace", "sandbox_name", "sandbox_uid", "runner_session_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("runner_session_id IS NOT NULL"),
    )
    op.create_index(
        "ux_session_open_key",
        "session",
        ["caller_namespace", "caller_name", "sandbox_namespace", "sandbox_name", "sandbox_uid", "open_key"],
        unique=True,
        postgresql_where=sa.text("open_key IS NOT NULL"),
    )
    op.create_index("ux_session_change_position", "session", ["change_position"], unique=True)


def downgrade() -> None:
    op.drop_table("session")
    op.execute(sa.schema.DropSequence(sa.Sequence("session_change_position")))
