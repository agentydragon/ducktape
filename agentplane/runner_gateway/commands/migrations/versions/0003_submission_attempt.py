"""Bounded command delivery over the runner channel, recorded on each submission."""

import sqlalchemy as sa
from alembic import op

revision = "0003_submission_attempt"
down_revision = "0002_runner_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("command_submission", sa.Column("attempt_sandbox_uid", sa.Uuid(), nullable=True))
    op.add_column("command_submission", sa.Column("attempt_runner_session_id", sa.String(), nullable=True))
    op.add_column("command_submission", sa.Column("attempt_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column("command_submission", sa.Column("attempt_sent_epoch", sa.BigInteger(), nullable=True))
    op.add_column("command_submission", sa.Column("attempt_refusal", sa.LargeBinary(), nullable=True))
    op.create_unique_constraint(
        "submission_attempt_destination",
        "command_submission",
        ["attempt_sandbox_uid", "attempt_runner_session_id", "command_id"],
    )
    op.create_check_constraint(
        "submission_attempt",
        "command_submission",
        "(attempt_sandbox_uid IS NULL AND attempt_runner_session_id IS NULL AND attempt_deadline IS NULL "
        "AND attempt_sent_epoch IS NULL AND attempt_refusal IS NULL) OR "
        "(attempt_sandbox_uid IS NOT NULL AND attempt_runner_session_id IS NOT NULL "
        "AND attempt_deadline IS NOT NULL)",
    )
    op.create_check_constraint(
        "attempt_sent_epoch_positive", "command_submission", "attempt_sent_epoch IS NULL OR attempt_sent_epoch > 0"
    )


def downgrade() -> None:
    op.drop_constraint("attempt_sent_epoch_positive", "command_submission", type_="check")
    op.drop_constraint("submission_attempt", "command_submission", type_="check")
    op.drop_constraint("submission_attempt_destination", "command_submission", type_="unique")
    for column in (
        "attempt_refusal",
        "attempt_sent_epoch",
        "attempt_deadline",
        "attempt_runner_session_id",
        "attempt_sandbox_uid",
    ):
        op.drop_column("command_submission", column)
