"""Replace ephemeral Sandbox ownership with independently fenced Session leases.

Stop old app replicas for this migration; they cannot use the new ownership scope.
Archive rows, app projections and checkpoints are unchanged. Leases are reacquired.
"""

from alembic import op

revision = "0022_session_projection_lease"
down_revision = "0021_projected_feed_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The exclusive table lock waits for old fenced writes before removing their authority.
    op.execute("DROP TABLE IF EXISTS sandbox_ingestion")
    # Historical revision replay tests may retain later schema changes.
    op.execute("""
        CREATE TABLE IF NOT EXISTS session_projection_lease (
            session_id uuid PRIMARY KEY REFERENCES event_log(id) ON DELETE CASCADE,
            token uuid NOT NULL,
            expires_at timestamptz NOT NULL
        )
    """)


def downgrade() -> None:
    # Ownership is ephemeral in both directions, never copied across fence domains.
    op.drop_table("session_projection_lease")
    op.execute("""
        CREATE TABLE sandbox_ingestion (
            sandbox text PRIMARY KEY,
            token uuid NOT NULL,
            expires_at timestamptz NOT NULL
        )
    """)
