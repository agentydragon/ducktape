"""A subject is one ServiceAccount, so the kind column and the Sandbox instance go.

`0002` carried the subject's kind beside its name because a Sandbox and a ServiceAccount could
share one. A Sandbox is no longer a subject, which leaves the namespace and the name as the whole
identity. Rows recorded against a Sandbox name no longer name anything a binding can reach, and
this history is diagnostic, so they are dropped rather than guessed at.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_service_account_subject"
down_revision = "0002_subject_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM egress_decision WHERE subject_kind = 'Sandbox'")
    op.drop_index("egress_decision_subject_recent", table_name="egress_decision")
    op.drop_constraint("egress_decision_subject_whole", "egress_decision", type_="check")
    op.drop_column("egress_decision", "subject_kind")
    op.drop_column("egress_decision", "sandbox_uid")
    op.create_check_constraint(
        "egress_decision_subject_whole", "egress_decision", "(subject_namespace IS NULL) = (subject_name IS NULL)"
    )
    op.create_index(
        "egress_decision_subject_recent",
        "egress_decision",
        ["subject_namespace", "subject_name", "decided_at", "event_id"],
    )


def downgrade() -> None:
    op.drop_index("egress_decision_subject_recent", table_name="egress_decision")
    op.drop_constraint("egress_decision_subject_whole", "egress_decision", type_="check")
    op.add_column("egress_decision", sa.Column("sandbox_uid", sa.Text()))
    op.add_column("egress_decision", sa.Column("subject_kind", sa.Text()))
    op.execute("UPDATE egress_decision SET subject_kind = 'ServiceAccount' WHERE subject_name IS NOT NULL")
    op.create_check_constraint(
        "egress_decision_subject_whole", "egress_decision", "(subject_kind IS NULL) = (subject_name IS NULL)"
    )
    op.create_index(
        "egress_decision_subject_recent", "egress_decision", ["subject_kind", "subject_name", "decided_at", "event_id"]
    )
