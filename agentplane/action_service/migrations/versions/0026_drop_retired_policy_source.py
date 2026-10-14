"""Delete the requests whose Decision names the retired `autoApproveIf` policy source.

`PolicySource` no longer has that member, so the reader raises on those Decisions' evidence and any
list or get that reaches one answers 500. Deleting a request cascades to its events, Decision and
Execution. Downgrade restores nothing.
"""

from alembic import op

revision = "0026_drop_retired_policy_source"
down_revision = "0025_drop_binding_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM action_request WHERE id IN (
            SELECT request_id FROM action_decision
            WHERE policy_evidence -> 'matched' ->> 'source' = 'autoApproveIf'
        )
        """
    )


def downgrade() -> None:
    pass
