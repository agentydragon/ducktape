"""The runner's journal deduplicates commands, so the gateway keeps no command submissions."""

import sqlalchemy as sa
from alembic import op

revision = "0010_command_submission_drop"
down_revision = "0009_session_open_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("command_submission")


def downgrade() -> None:
    # Empty: a downgraded gateway reads no row as no command submitted yet.
    op.create_table(
        "command_submission",
        sa.Column("session_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("command_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("runner_command", sa.LargeBinary(), nullable=False),
        sa.Column("caller_namespace", sa.String(), nullable=False),
        sa.Column("caller_name", sa.String(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("admission", sa.LargeBinary(), nullable=True),
        sa.Column("rejection", sa.String(512), nullable=True),
        sa.Column("attempt_sandbox_uid", sa.Uuid(), nullable=True),
        sa.Column("attempt_runner_session_id", sa.String(), nullable=True),
        sa.Column("attempt_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_sent_epoch", sa.BigInteger(), nullable=True),
        sa.Column("attempt_refusal", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint("state IN ('pending_admission', 'admitted', 'rejected')", name="submission_state"),
        sa.CheckConstraint(
            "(state = 'admitted' AND admission IS NOT NULL AND rejection IS NULL) OR "
            "(state = 'pending_admission' AND admission IS NULL AND rejection IS NULL) OR "
            "(state = 'rejected' AND admission IS NULL AND rejection IS NOT NULL)",
            name="submission_evidence",
        ),
        sa.CheckConstraint(
            "(attempt_sandbox_uid IS NULL AND attempt_runner_session_id IS NULL AND attempt_deadline IS NULL "
            "AND attempt_sent_epoch IS NULL AND attempt_refusal IS NULL) OR "
            "(attempt_sandbox_uid IS NOT NULL AND attempt_runner_session_id IS NOT NULL "
            "AND attempt_deadline IS NOT NULL)",
            name="submission_attempt",
        ),
        sa.CheckConstraint("attempt_sent_epoch IS NULL OR attempt_sent_epoch > 0", name="attempt_sent_epoch_positive"),
        sa.UniqueConstraint(
            "attempt_sandbox_uid", "attempt_runner_session_id", "command_id", name="submission_attempt_destination"
        ),
    )
