"""Share the existing human decision note with caller and operator without losing data."""

from alembic import op

revision = "0006_decision_note"
down_revision = "0005_structured_action"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("action_decision", "private_reason", new_column_name="decision_note")


def downgrade() -> None:
    op.alter_column("action_decision", "decision_note", new_column_name="private_reason")
