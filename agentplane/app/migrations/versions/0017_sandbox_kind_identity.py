"""Carry the Sandbox provider through persisted Thread and ingestion identities.

Existing event logs and leases become Agent Sandboxes in place. Kind remains in the event log so a
Thread can still find its runner after the VM or Sandbox is deleted.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_sandbox_kind_identity"
down_revision = "0016_thread_payload_chunk_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_log", sa.Column("sandbox_kind", sa.Text(), server_default=sa.text("'agent_sandbox'"), nullable=False)
    )
    op.drop_constraint("event_log_sandbox_session_id_key", "event_log", type_="unique")
    op.create_unique_constraint(
        "uq_event_log_sandbox_kind_session", "event_log", ["sandbox_kind", "sandbox", "session_id"]
    )

    op.add_column(
        "sandbox_ingestion",
        sa.Column("sandbox_kind", sa.Text(), server_default=sa.text("'agent_sandbox'"), nullable=False),
    )
    op.drop_constraint("sandbox_ingestion_pkey", "sandbox_ingestion", type_="primary")
    op.create_primary_key("sandbox_ingestion_pkey", "sandbox_ingestion", ["sandbox_kind", "sandbox"])


def downgrade() -> None:
    # A name-only identity cannot represent a VM Thread or a lease beside an Agent Sandbox of the
    # same name. Refuse the downgrade rather than discard the only record of which runner owns it.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM event_log WHERE sandbox_kind <> 'agent_sandbox') THEN
                RAISE EXCEPTION 'cannot remove sandbox kind while VM Threads are stored';
            END IF;
            IF EXISTS (SELECT 1 FROM sandbox_ingestion WHERE sandbox_kind <> 'agent_sandbox') THEN
                RAISE EXCEPTION 'cannot remove sandbox kind while VM ingestion leases are stored';
            END IF;
        END $$
        """
    )
    op.drop_constraint("sandbox_ingestion_pkey", "sandbox_ingestion", type_="primary")
    op.create_primary_key("sandbox_ingestion_pkey", "sandbox_ingestion", ["sandbox"])
    op.drop_column("sandbox_ingestion", "sandbox_kind")

    op.drop_constraint("uq_event_log_sandbox_kind_session", "event_log", type_="unique")
    op.create_unique_constraint(None, "event_log", ["sandbox", "session_id"])
    op.drop_column("event_log", "sandbox_kind")
