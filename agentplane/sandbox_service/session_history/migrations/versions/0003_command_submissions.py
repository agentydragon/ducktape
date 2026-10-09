"""Service-owned command submissions; draft, blocked on archive-ownership rollout."""

import sqlalchemy as sa
from alembic import op

revision = "0003_command_submissions"
down_revision = "0002_session_open_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_submission",
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("session_history.id"), primary_key=True, nullable=False),
        sa.Column("command_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("runner_command", sa.LargeBinary(), nullable=False),
        sa.Column("caller_namespace", sa.String(), nullable=False),
        sa.Column("caller_name", sa.String(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("admission", sa.LargeBinary(), nullable=True),
        sa.Column("rejection", sa.String(512), nullable=True),
        sa.CheckConstraint("state IN ('pending_admission', 'admitted', 'rejected')", name="submission_state"),
        sa.CheckConstraint(
            "(state = 'admitted' AND admission IS NOT NULL AND rejection IS NULL) OR "
            "(state = 'pending_admission' AND admission IS NULL AND rejection IS NULL) OR "
            "(state = 'rejected' AND admission IS NULL AND rejection IS NOT NULL)",
            name="submission_evidence",
        ),
    )


def downgrade() -> None:
    op.drop_table("command_submission")
