"""Create the complete Action approval Web Push schema in one revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_action_push_subscription"
down_revision = "0011_enrollment_reconnect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_push_subscription",
        sa.Column("endpoint", sa.Text(), primary_key=True),
        sa.Column("operator_principal", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_action_push_subscription_operator_principal", "action_push_subscription", ["operator_principal"]
    )
    op.create_table(
        "action_push_delivery",
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("action_request.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "endpoint",
            sa.Text(),
            sa.ForeignKey("action_push_subscription.endpoint", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("request_id", "endpoint"),
    )


def downgrade() -> None:
    op.drop_table("action_push_delivery")
    op.drop_index("ix_action_push_subscription_operator_principal", table_name="action_push_subscription")
    op.drop_table("action_push_subscription")
