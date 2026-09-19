"""A decision's subject is one namespaced ServiceAccount.

`0001` recorded the only subject the proxy could authenticate: a Sandbox, by name, namespace and
UID. A subject is now the ServiceAccount a caller's Pod runs as, so the namespace and the name are
the whole identity and a check constraint keeps the pair from being half-written.

Existing rows name a Sandbox, which is no longer anything a binding can reach. This history is
diagnostic, so they are dropped rather than guessed at -- in both directions.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_service_account_subject"
down_revision = "0001_decisions"
branch_labels = None
depends_on = None

_WHOLE = "(subject_namespace IS NULL) = (subject_name IS NULL)"


def upgrade() -> None:
    op.execute("DELETE FROM egress_decision")
    op.drop_index("egress_decision_subject_recent", table_name="egress_decision")
    op.alter_column("egress_decision", "sandbox", new_column_name="subject_name")
    op.alter_column("egress_decision", "sandbox_namespace", new_column_name="subject_namespace")
    op.drop_column("egress_decision", "sandbox_uid")
    op.create_check_constraint("egress_decision_subject_whole", "egress_decision", _WHOLE)
    op.create_index(
        "egress_decision_subject_recent",
        "egress_decision",
        ["subject_namespace", "subject_name", "decided_at", "event_id"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM egress_decision")
    op.drop_index("egress_decision_subject_recent", table_name="egress_decision")
    op.drop_constraint("egress_decision_subject_whole", "egress_decision", type_="check")
    op.add_column("egress_decision", sa.Column("sandbox_uid", sa.Text()))
    op.alter_column("egress_decision", "subject_name", new_column_name="sandbox")
    op.alter_column("egress_decision", "subject_namespace", new_column_name="sandbox_namespace")
    op.create_index("egress_decision_subject_recent", "egress_decision", ["sandbox", "decided_at", "event_id"])
