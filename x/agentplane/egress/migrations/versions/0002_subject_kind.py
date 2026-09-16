"""Record which kind of subject a decision was for, not just a Sandbox name.

`sandbox` held the name of the only subject kind the proxy could authenticate. A ServiceAccount is
now one too, and the two share a namespace of names, so a name alone no longer identifies who a
decision was about: the kind travels beside it and a check constraint keeps the pair whole.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_subject_kind"
down_revision = "0001_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("egress_decision", "sandbox", new_column_name="subject_name")
    op.alter_column("egress_decision", "sandbox_namespace", new_column_name="subject_namespace")
    op.add_column("egress_decision", sa.Column("subject_kind", sa.Text()))
    # Every row written before this migration was about a Sandbox; only that kind could authenticate.
    op.execute("UPDATE egress_decision SET subject_kind = 'Sandbox' WHERE subject_name IS NOT NULL")
    op.create_check_constraint(
        "egress_decision_subject_whole", "egress_decision", "(subject_kind IS NULL) = (subject_name IS NULL)"
    )
    op.drop_index("egress_decision_subject_recent", table_name="egress_decision")
    op.create_index(
        "egress_decision_subject_recent", "egress_decision", ["subject_kind", "subject_name", "decided_at", "event_id"]
    )


def downgrade() -> None:
    op.drop_index("egress_decision_subject_recent", table_name="egress_decision")
    op.drop_constraint("egress_decision_subject_whole", "egress_decision", type_="check")
    # A ServiceAccount subject has no column to go back to; the name alone would read as a Sandbox.
    op.execute("DELETE FROM egress_decision WHERE subject_kind = 'ServiceAccount'")
    op.drop_column("egress_decision", "subject_kind")
    op.alter_column("egress_decision", "subject_namespace", new_column_name="sandbox_namespace")
    op.alter_column("egress_decision", "subject_name", new_column_name="sandbox")
    op.create_index("egress_decision_subject_recent", "egress_decision", ["sandbox", "decided_at", "event_id"])
