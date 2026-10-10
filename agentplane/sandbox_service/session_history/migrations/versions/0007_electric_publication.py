"""Publish the History Service fold's synced tables to Electric, as the app database does its own.

The `electric` role is the cluster's replication login; Electric reads only these tables.
"""

from alembic import op

revision = "0007_electric_publication"
down_revision = "0006_summary_first_event"
branch_labels = None
depends_on = None

SYNCED = ("thread_checkpoint", "thread_entity", "thread_payload_manifest", "thread_payload_chunk")


def upgrade() -> None:
    for table in SYNCED:
        op.execute(f"ALTER TABLE {table} REPLICA IDENTITY FULL")
    op.execute("GRANT USAGE ON SCHEMA public TO electric")
    op.execute(f"GRANT SELECT ON {', '.join(SYNCED)} TO electric")
    op.execute(f"CREATE PUBLICATION electric_publication_agentplane_history FOR TABLE {', '.join(SYNCED)}")


def downgrade() -> None:
    op.execute("DROP PUBLICATION IF EXISTS electric_publication_agentplane_history")
    op.execute(f"REVOKE SELECT ON {', '.join(SYNCED)} FROM electric")
    op.execute("REVOKE USAGE ON SCHEMA public FROM electric")
    for table in SYNCED:
        op.execute(f"ALTER TABLE {table} REPLICA IDENTITY DEFAULT")
