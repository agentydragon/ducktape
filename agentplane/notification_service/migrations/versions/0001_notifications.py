"""Create notification subscriptions, retained inboxes, and recoverable notice state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_notifications"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox",
        sa.UniqueConstraint("owner_namespace", "owner_name", "destination_key"),
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("owner_namespace", sa.String(), primary_key=False, nullable=False),
        sa.Column("owner_name", sa.String(), primary_key=False, nullable=False),
        sa.Column("destination_key", sa.String(), primary_key=False, nullable=False),
        sa.Column("destination_ref", postgresql.JSONB(), primary_key=False, nullable=False),
        sa.Column("session_id", sa.String(), primary_key=False, nullable=False),
        sa.Column("last_cursor", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("acknowledged", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("covered", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("expired_through", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("retired", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column("next_poll", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column("claim", sa.Uuid(), primary_key=False, nullable=True),
        sa.Column("claim_until", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column("delivery_error", sa.String(), primary_key=False, nullable=True),
    )
    op.create_index("ix_inbox_next_poll", "inbox", ["next_poll"])
    op.create_table(
        "subscription",
        sa.UniqueConstraint("inbox_id", "client_key"),
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "inbox_id", sa.Uuid(), sa.ForeignKey("inbox.id", ondelete="CASCADE"), primary_key=False, nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), primary_key=False, nullable=False),
        sa.Column("client_key", sa.String(), primary_key=False, nullable=False),
        sa.Column("creation", postgresql.JSONB(), primary_key=False, nullable=False),
        sa.Column("creator", postgresql.JSONB(), primary_key=False, nullable=False),
        sa.Column("version", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("after_sequence", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("paused", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("cancelled", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column("next_poll", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column("error", sa.String(), primary_key=False, nullable=True),
    )
    op.create_table(
        "entry",
        sa.UniqueConstraint("inbox_id", "request_id", "source_sequence"),
        sa.Column(
            "inbox_id", sa.Uuid(), sa.ForeignKey("inbox.id", ondelete="CASCADE"), primary_key=True, nullable=False
        ),
        sa.Column("cursor", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("request_id", sa.Uuid(), primary_key=False, nullable=False),
        sa.Column("source_sequence", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("payload", postgresql.JSONB(), primary_key=False, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
    )
    op.create_table(
        "subscription_match",
        sa.Column(
            "subscription_id",
            sa.Uuid(),
            sa.ForeignKey("subscription.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("cursor", sa.BigInteger(), primary_key=True, nullable=False),
    )
    op.create_table(
        "notice",
        sa.Column(
            "inbox_id", sa.Uuid(), sa.ForeignKey("inbox.id", ondelete="CASCADE"), primary_key=True, nullable=False
        ),
        sa.Column("command_id", sa.Uuid(), primary_key=False, nullable=False),
        sa.Column("through_cursor", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("text", sa.String(), primary_key=False, nullable=False),
        sa.Column("attempted", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("admitted", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("confirmed", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("error", sa.String(), primary_key=False, nullable=True),
        sa.Column("runner_cursor", sa.BigInteger(), primary_key=False, nullable=False),
        sa.Column("runner_entry", sa.LargeBinary(), primary_key=False, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("notice")
    op.drop_table("subscription_match")
    op.drop_table("entry")
    op.drop_table("subscription")
    op.drop_table("inbox")
