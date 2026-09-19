"""Append-only egress diagnostic admissions, independently migrated."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_decisions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "egress_decision",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("producer_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox", sa.Text()),
        sa.Column("sandbox_namespace", sa.Text()),
        sa.Column("sandbox_uid", sa.Text()),
        sa.Column("source_pod_uid", sa.Text()),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("binding", sa.Text()),
        sa.Column("policy", sa.Text()),
        sa.Column("rule", sa.Integer()),
        sa.Column("substituted", sa.Boolean(), nullable=False),
        sa.Column("address", postgresql.INET()),
    )
    op.create_index("egress_decision_subject_recent", "egress_decision", ["sandbox", "decided_at", "event_id"])
    op.create_index("egress_decision_denials_recent", "egress_decision", ["outcome", "decided_at", "event_id"])
    op.create_index("egress_decision_retention", "egress_decision", ["decided_at", "event_id"])


def downgrade() -> None:
    op.drop_table("egress_decision")
