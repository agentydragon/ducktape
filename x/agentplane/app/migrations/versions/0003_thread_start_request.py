"""Persist an idempotent Thread-start request before a Sandbox runner exists."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_thread_start_request"
down_revision = "0002_thread_archived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_start_request",
        # The desired product Thread identity. New Sandboxes carry this value as their correlation
        # label, so no separate launch/request identifier is needed.
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        # An existing target has sandbox + UID; a new target has sandbox_creation and is located by
        # the thread-id label once its Kubernetes object exists.
        sa.Column("sandbox", sa.Text(), nullable=True),
        sa.Column("sandbox_uid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sandbox_creation", postgresql.JSONB(none_as_null=True), nullable=True),
        # The first runner attachment only. A Thread may later have other runner sessions.
        sa.Column("runner_session_id", sa.Text(), nullable=False),
        sa.Column("session_spec", postgresql.JSONB(), nullable=False),
        sa.Column("input_id", sa.Text(), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("thread_id"),
        sa.CheckConstraint(
            "(sandbox IS NOT NULL AND sandbox_uid IS NOT NULL AND sandbox_creation IS NULL) "
            "OR (sandbox IS NULL AND sandbox_uid IS NULL AND sandbox_creation IS NOT NULL)",
            name="thread_start_request_exactly_one_sandbox_target",
        ),
    )


def downgrade() -> None:
    op.drop_table("thread_start_request")
