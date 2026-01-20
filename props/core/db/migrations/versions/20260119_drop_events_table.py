"""Drop events table and related views.

With the proxy architecture, agents run inside containers and cannot write
events directly to the database. All LLM requests are now logged via the
proxy into the llm_requests table instead.

This migration removes:
- event_costs view (computed costs from events)
- run_costs view (aggregated costs from event_costs)
- events table RLS policies
- events table

The llm_request_costs and llm_run_costs views (from 20260118 migration)
now provide equivalent functionality.

Revision ID: 20260119_drop_events_table
Revises: 20260118_llm_requests_and_container_logs
Create Date: 2026-01-19
"""

from alembic import op

revision = "20260119_drop_events_table"
down_revision = "20260118_llm_requests_and_container_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop events infrastructure."""
    # Drop views that depend on events table (order matters - run_costs depends on event_costs)
    op.execute("DROP VIEW IF EXISTS run_costs CASCADE")
    op.execute("DROP VIEW IF EXISTS event_costs CASCADE")

    # Drop RLS policies on events table
    op.execute("DROP POLICY IF EXISTS admin_full_access_events ON events")
    op.execute("DROP POLICY IF EXISTS events_agent_select ON events")

    # Drop the events table
    op.drop_table("events")


def downgrade() -> None:
    """Recreate events table and views.

    Note: This only recreates the schema structure. Historical event data
    cannot be recovered.
    """
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

    # Recreate events table
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_run_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_num", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.agent_run_id"], ondelete="CASCADE", name="fk_events_agent_run_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "sequence_num", name="uq_events_agent_run_id_seq"),
    )
    op.create_index("ix_events_agent_run_id_seq", "events", ["agent_run_id", "sequence_num"])

    # Recreate event_costs view
    op.execute("""
        CREATE VIEW event_costs AS
        SELECT
            (events.payload->'response_id')::text AS response_id,
            events.agent_run_id,
            ((events.payload->'usage'->'model')::text) AS model,
            ((events.payload->'usage'->'input_tokens')::text)::integer AS input_tokens,
            COALESCE(((events.payload->'usage'->'input_tokens_details'->'cached_tokens')::text)::integer, 0) AS cached_tokens,
            ((events.payload->'usage'->'output_tokens')::text)::integer AS output_tokens,
            COALESCE(((events.payload->'usage'->'output_tokens_details'->'reasoning_tokens')::text)::integer, 0) AS reasoning_tokens,
            (
                (((events.payload->'usage'->'input_tokens')::text)::integer -
                 COALESCE(((events.payload->'usage'->'input_tokens_details'->'cached_tokens')::text)::integer, 0))::float
                    * model_metadata.input_usd_per_1m_tokens / 1000000
                + COALESCE(((events.payload->'usage'->'input_tokens_details'->'cached_tokens')::text)::integer, 0)::float
                    * model_metadata.cached_input_usd_per_1m_tokens / 1000000
                + ((events.payload->'usage'->'output_tokens')::text)::integer::float
                    * model_metadata.output_usd_per_1m_tokens / 1000000
            ) AS cost_usd,
            events.timestamp
        FROM events
        JOIN model_metadata ON ((events.payload->'usage'->'model')::text) = model_metadata.model_id
        WHERE events.event_type = 'response' AND events.payload->'usage' IS NOT NULL
    """)

    # Recreate run_costs view
    op.execute("""
        CREATE VIEW run_costs AS
        WITH RECURSIVE run_tree AS (
            SELECT agent_run_id, agent_run_id AS root_run_id
            FROM agent_runs
            UNION ALL
            SELECT ar.agent_run_id, rt.root_run_id
            FROM agent_runs ar
            JOIN run_tree rt ON ar.parent_agent_run_id = rt.agent_run_id
        )
        SELECT
            rt.root_run_id AS agent_run_id,
            ec.model,
            SUM(ec.input_tokens) AS input_tokens,
            SUM(ec.cached_tokens) AS cached_tokens,
            SUM(ec.output_tokens) AS output_tokens,
            SUM(ec.reasoning_tokens) AS reasoning_tokens,
            SUM(ec.cost_usd) AS cost_usd
        FROM run_tree rt
        JOIN event_costs ec ON ec.agent_run_id = rt.agent_run_id
        GROUP BY rt.root_run_id, ec.model
    """)

    # Recreate RLS
    op.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY admin_full_access_events ON events TO postgres USING (true) WITH CHECK (true)")
    op.execute("""
        CREATE POLICY events_agent_select ON events FOR SELECT USING (
            (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(agent_run_id))
            OR (agent_run_id = current_agent_run_id())
            OR (current_agent_type() = 'improvement'
                AND agent_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)
