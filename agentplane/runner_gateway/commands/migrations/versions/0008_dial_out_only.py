"""Runners dial out and push their Events, so the registry keeps no feed, connection record or locator.

Nothing watches Sessions any more: every runner pushes its Events to the History ingress. Every runner
connection carries every control operation, so nothing records which ones it agreed. The runner's
Session ID is the Session ID, so the registry keeps no separate runner locator.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_dial_out_only"
down_revision = "0007_redelivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ux_session_change_position", table_name="session")
    op.drop_column("session", "change_position")
    op.execute(sa.schema.DropSequence(sa.Sequence("session_change_position")))
    op.drop_table("runner_connection")
    op.drop_index("ux_session_runner_locator", table_name="session")
    op.drop_column("session", "runner_session_id")


def downgrade() -> None:
    op.add_column("session", sa.Column("runner_session_id", sa.String(), nullable=True))
    # A downgraded gateway resolves runner Sessions through the locator; every Session's is its ID.
    op.execute("UPDATE session SET runner_session_id = id::text")
    op.create_index(
        "ux_session_runner_locator",
        "session",
        ["sandbox_namespace", "sandbox_name", "sandbox_uid", "runner_session_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("runner_session_id IS NOT NULL"),
    )
    # Empty: a downgraded gateway reads no row as no operation agreed yet.
    op.create_table(
        "runner_connection",
        sa.Column("sandbox_uid", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("pod_uid", sa.String(), nullable=True),
        sa.Column("connection_epoch", sa.BigInteger(), nullable=True),
        sa.Column("owner_replica", sa.String(), nullable=True),
        sa.Column("runner_boot_id", sa.String(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operations", postgresql.ARRAY(sa.String()), nullable=False),
        sa.CheckConstraint("connection_epoch > 0", name="connection_epoch_positive"),
    )
    # Positions restart in table order; a watcher resumes from zero.
    op.execute(sa.schema.CreateSequence(sa.Sequence("session_change_position")))
    op.add_column(
        "session",
        sa.Column(
            "change_position",
            sa.BigInteger(),
            server_default=sa.text("nextval('session_change_position')"),
            nullable=False,
        ),
    )
    op.alter_column("session", "change_position", server_default=None)
    op.create_index("ux_session_change_position", "session", ["change_position"], unique=True)
