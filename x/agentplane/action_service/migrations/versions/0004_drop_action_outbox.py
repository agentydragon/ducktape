"""Drop the unused pending-decision outbox; the durable Action event/query surface is delivery."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_drop_action_outbox"
down_revision = "0003_executor_liveness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("action_outbox")


def downgrade() -> None:
    op.create_table(
        "action_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("action_request.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("request_id", "kind"),
    )
    op.create_index(
        "ix_action_outbox_pending", "action_outbox", ["created_at"], postgresql_where=sa.text("delivered_at IS NULL")
    )
