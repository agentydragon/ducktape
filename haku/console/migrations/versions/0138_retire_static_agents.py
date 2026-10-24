"""Revoke deployment-owned static Agent credentials.

Static Agents are no longer configured or reconciled by the Console. Keep their Agent and
credential rows as history, while ensuring no static credential remains active after rollout.

Revision ID: 0138
Revises: 0137
"""

from alembic import op

revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH revoked_static_bindings AS (
            UPDATE credential_bindings
            SET status = 'revoked',
                ended_at = now(),
                end_reason = 'static_agents_retired',
                updated_at = now()
            WHERE kind = 'static' AND status = 'active'
            RETURNING agent_id
        )
        UPDATE agents
        SET status = 'disabled', updated_at = now()
        FROM revoked_static_bindings
        WHERE agents.agent_id = revoked_static_bindings.agent_id
          AND agents.status = 'active'
        """
    )


def downgrade() -> None:
    raise RuntimeError("0138 revoked retired static Agent credentials; they cannot be restored")
