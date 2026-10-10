"""Record settled streamed deltas and the per-session settlement override."""

import sqlalchemy as sa
from alembic import op

revision = "0004_delta_settlement"
down_revision = "0003_history_feed_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_history", sa.Column("settle_deltas", sa.Boolean(), nullable=True))
    op.create_table(
        "session_event_settlement",
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("session_history.id"), primary_key=True, nullable=False),
        sa.Column("completion_cursor", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        "session_event_settled_range",
        sa.Column("session_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("first_cursor", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("last_cursor", sa.BigInteger(), nullable=False),
        sa.Column("completion_cursor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("first_cursor <= last_cursor", name="settled_range_ordered"),
        sa.ForeignKeyConstraint(
            ["session_id", "completion_cursor"],
            ["session_event_settlement.session_id", "session_event_settlement.completion_cursor"],
            name="settled_range_settlement",
        ),
    )


def downgrade() -> None:
    op.drop_table("session_event_settled_range")
    op.drop_table("session_event_settlement")
    op.drop_column("session_history", "settle_deltas")
