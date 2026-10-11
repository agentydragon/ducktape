"""Stop publishing the app's fold tables to Electric, which now reads only the History Service fold.

The `electric` role owns the replication slot, so this role cannot drop it; it is dropped with the
role's own privileges once the app Electric deployment is gone (see the app README).
"""

from alembic import op

revision = "0031_app_electric_retire"
down_revision = "0030_thread_resume"
branch_labels = None
depends_on = None

SYNCED = ("thread_checkpoint", "thread_entity", "thread_payload_manifest", "thread_payload_chunk")


def upgrade() -> None:
    op.execute("DROP PUBLICATION IF EXISTS electric_publication_agentplane_conversation")
    op.execute(f"REVOKE SELECT ON {', '.join(SYNCED)} FROM electric")
    op.execute("REVOKE USAGE ON SCHEMA public FROM electric")


def downgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA public TO electric")
    op.execute(f"GRANT SELECT ON {', '.join(SYNCED)} TO electric")
    op.execute(f"CREATE PUBLICATION electric_publication_agentplane_conversation FOR TABLE {', '.join(SYNCED)}")
