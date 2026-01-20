"""In-container agent loop model schema changes.

Revision ID: 20260120_in_container_agent_loop
Revises: 20260113_proxy_agent_definitions
Create Date: 2026-01-20

Combined migration for the in-container agent loop model:
1. llm_requests table (replaces events for LLM tracking via proxy)
2. Container observability (stdout/stderr/exit_code in agent_runs)
3. Drop events table (no longer used with proxy architecture)
4. Critique notification triggers (for grader daemon)
5. Resource limits and lifecycle columns in agent_runs
6. Updated notification format
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision = "20260120_in_container_agent_loop"
down_revision = "20260113_proxy_agent_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # 1. Create llm_requests table (replaces events for LLM call tracking)
    # =========================================================================
    op.create_table(
        "llm_requests",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "agent_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("model", sa.String, nullable=False, index=True),
        sa.Column("request_body", JSONB, nullable=False),
        sa.Column("response_body", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("cached_input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP, nullable=False, server_default=sa.func.now()),
        comment="LLM API requests logged by the proxy. Replaces events table for LLM tracking.",
    )

    op.create_index("ix_llm_requests_agent_run_created", "llm_requests", ["agent_run_id", "created_at"])

    # Enable RLS on llm_requests
    op.execute("ALTER TABLE llm_requests ENABLE ROW LEVEL SECURITY")

    # Helper function to check ancestor relationship
    op.execute("""
        CREATE OR REPLACE FUNCTION is_agent_ancestor(ancestor_id UUID, descendant_id UUID)
        RETURNS BOOLEAN AS $$
        WITH RECURSIVE ancestors AS (
            SELECT agent_run_id, parent_agent_run_id
            FROM agent_runs
            WHERE agent_run_id = descendant_id
            UNION ALL
            SELECT ar.agent_run_id, ar.parent_agent_run_id
            FROM agent_runs ar
            JOIN ancestors a ON ar.agent_run_id = a.parent_agent_run_id
        )
        SELECT EXISTS (
            SELECT 1 FROM ancestors WHERE agent_run_id = ancestor_id
        );
        $$ LANGUAGE SQL STABLE SECURITY DEFINER;
    """)

    # RLS policies for llm_requests (with split-based access)
    op.execute("""
        CREATE POLICY llm_requests_select ON llm_requests FOR SELECT USING (
            current_agent_run_id() IS NULL
            OR is_agent_ancestor(current_agent_run_id(), agent_run_id)
            OR (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(agent_run_id))
            OR (current_agent_type() = 'improvement'
                AND agent_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)

    op.execute("""
        CREATE POLICY llm_requests_insert ON llm_requests FOR INSERT WITH CHECK (
            current_agent_run_id() IS NULL
        )
    """)

    # Views for cost computation
    op.execute("""
        CREATE OR REPLACE VIEW llm_request_costs AS
        SELECT
            r.id,
            r.agent_run_id,
            r.model,
            r.input_tokens,
            r.cached_input_tokens,
            r.output_tokens,
            r.latency_ms,
            r.created_at,
            COALESCE(
                (r.input_tokens - COALESCE(r.cached_input_tokens, 0))
                    * m.input_usd_per_1m_tokens / 1000000.0
                + COALESCE(r.cached_input_tokens, 0)
                    * m.cached_input_usd_per_1m_tokens / 1000000.0
                + r.output_tokens
                    * m.output_usd_per_1m_tokens / 1000000.0,
                0
            ) AS cost_usd
        FROM llm_requests r
        LEFT JOIN model_metadata m ON r.model = m.model_id
    """)

    # run_costs view - matches the name from pre-proxy events-based implementation
    op.execute("""
        CREATE OR REPLACE VIEW run_costs AS
        SELECT
            agent_run_id,
            model,
            SUM(input_tokens) AS input_tokens,
            SUM(cached_input_tokens) AS cached_input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(cost_usd) AS cost_usd,
            COUNT(*) AS request_count
        FROM llm_request_costs
        GROUP BY agent_run_id, model
    """)

    # =========================================================================
    # 2. Add container observability columns to agent_runs
    # =========================================================================
    op.add_column("agent_runs", sa.Column("container_stdout", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("container_stderr", sa.Text(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column(
            "container_exit_code",
            sa.Integer(),
            nullable=True,
            comment="Container exit code (NULL if still running or not container-based)",
        ),
    )

    # =========================================================================
    # 3. Add resource limits and lifecycle columns
    # =========================================================================
    op.add_column(
        "agent_runs",
        sa.Column(
            "budget_usd",
            sa.Float(),
            nullable=True,
            comment="Max USD cost allowed for this agent (including child agents). Enforced by proxy.",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            nullable=True,
            comment="Max seconds before agent is killed. Enforced by agent_registry.",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("started_at", TIMESTAMP(timezone=True), nullable=True, comment="When container started executing"),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "ended_at", TIMESTAMP(timezone=True), nullable=True, comment="When container finished (success or failure)"
        ),
    )

    # Drop completion_summary column (no longer used)
    op.drop_column("agent_runs", "completion_summary")

    # =========================================================================
    # 4. Drop events table (replaced by llm_requests)
    # =========================================================================
    op.execute("DROP VIEW IF EXISTS run_costs CASCADE")
    op.execute("DROP VIEW IF EXISTS event_costs CASCADE")
    op.execute("DROP POLICY IF EXISTS admin_full_access_events ON events")
    op.execute("DROP POLICY IF EXISTS events_agent_select ON events")
    op.drop_table("events")

    # =========================================================================
    # 5. Create critique notification triggers
    # =========================================================================
    # Update GT notification trigger with new format
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_row RECORD;
            v_item JSONB;
        BEGIN
            v_row := COALESCE(NEW, OLD);
            v_item := json_build_object('table', TG_TABLE_NAME);

            CASE TG_TABLE_NAME
                WHEN 'true_positives' THEN
                    v_item := v_item || json_build_object('tp_id', v_row.tp_id);
                WHEN 'true_positive_occurrences' THEN
                    v_item := v_item || json_build_object('tp_id', v_row.tp_id, 'occurrence_id', v_row.occurrence_id);
                WHEN 'false_positives' THEN
                    v_item := v_item || json_build_object('fp_id', v_row.fp_id);
                WHEN 'false_positive_occurrences' THEN
                    v_item := v_item || json_build_object('fp_id', v_row.fp_id, 'occurrence_id', v_row.occurrence_id);
            END CASE;

            PERFORM pg_notify('grading_pending', json_build_object(
                'operation', TG_OP,
                'item', v_item,
                'snapshot_slug', v_row.snapshot_slug
            )::text);
            RETURN v_row;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Create critique notification trigger function
    op.execute("""
        CREATE FUNCTION notify_critique_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_snapshot_slug VARCHAR;
            v_item JSONB;
        BEGIN
            SELECT ar.type_config->'example'->>'snapshot_slug'
            INTO v_snapshot_slug
            FROM agent_runs ar
            WHERE ar.agent_run_id = NEW.agent_run_id;

            IF v_snapshot_slug IS NOT NULL THEN
                v_item := json_build_object('table', TG_TABLE_NAME);

                CASE TG_TABLE_NAME
                    WHEN 'reported_issues' THEN
                        v_item := v_item || json_build_object(
                            'agent_run_id', NEW.agent_run_id,
                            'issue_id', NEW.issue_id
                        );
                    WHEN 'reported_issue_occurrences' THEN
                        v_item := v_item || json_build_object(
                            'occurrence_id', NEW.id,
                            'agent_run_id', NEW.agent_run_id,
                            'reported_issue_id', NEW.reported_issue_id
                        );
                END CASE;

                PERFORM pg_notify('grading_pending', json_build_object(
                    'operation', TG_OP,
                    'item', v_item,
                    'snapshot_slug', v_snapshot_slug
                )::text);
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        COMMENT ON FUNCTION notify_critique_changed() IS
        'Sends pg_notify when new critiques are reported. Used to wake snapshot_grader daemons.'
    """)

    # Triggers on critique tables
    op.execute("""
        CREATE TRIGGER trg_notify_reported_issue_changed
        AFTER INSERT ON reported_issues
        FOR EACH ROW EXECUTE FUNCTION notify_critique_changed()
    """)

    op.execute("""
        CREATE TRIGGER trg_notify_reported_issue_occ_changed
        AFTER INSERT ON reported_issue_occurrences
        FOR EACH ROW EXECUTE FUNCTION notify_critique_changed()
    """)


def downgrade() -> None:
    # Remove critique triggers
    op.execute("DROP TRIGGER IF EXISTS trg_notify_reported_issue_occ_changed ON reported_issue_occurrences")
    op.execute("DROP TRIGGER IF EXISTS trg_notify_reported_issue_changed ON reported_issues")
    op.execute("DROP FUNCTION IF EXISTS notify_critique_changed()")

    # Restore original GT trigger function format
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('grading_pending', json_build_object(
                'event', TG_OP || '_' || TG_TABLE_NAME,
                'snapshot_slug', COALESCE(NEW.snapshot_slug, OLD.snapshot_slug)
            )::text);
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)

    # Recreate events table
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_num", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.agent_run_id"], ondelete="CASCADE", name="fk_events_agent_run_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "sequence_num", name="uq_events_agent_run_id_seq"),
    )
    op.create_index("ix_events_agent_run_id_seq", "events", ["agent_run_id", "sequence_num"])

    # Recreate event_costs and run_costs views
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

    # Recreate events RLS
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

    # Restore completion_summary column
    op.add_column("agent_runs", sa.Column("completion_summary", sa.Text(), nullable=True))

    # Drop lifecycle and resource columns
    op.drop_column("agent_runs", "ended_at")
    op.drop_column("agent_runs", "started_at")
    op.drop_column("agent_runs", "timeout_seconds")
    op.drop_column("agent_runs", "budget_usd")

    # Drop container observability columns
    op.drop_column("agent_runs", "container_exit_code")
    op.drop_column("agent_runs", "container_stderr")
    op.drop_column("agent_runs", "container_stdout")

    # Drop llm_requests infrastructure
    op.execute("DROP VIEW IF EXISTS run_costs")
    op.execute("DROP VIEW IF EXISTS llm_request_costs")
    op.execute("DROP POLICY IF EXISTS llm_requests_insert ON llm_requests")
    op.execute("DROP POLICY IF EXISTS llm_requests_select ON llm_requests")
    op.drop_table("llm_requests")
    op.execute("DROP FUNCTION IF EXISTS is_agent_ancestor(UUID, UUID)")
