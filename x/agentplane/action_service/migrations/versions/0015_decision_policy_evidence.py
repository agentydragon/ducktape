"""Record on a policy-set Decision the bindings, sets and leaf policy it evaluated at admission."""

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_decision_policy_evidence"
down_revision = "0014_grant_caller"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_decision", Column("policy_evidence", JSONB(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("action_decision", "policy_evidence")
