"""Add llm_requests table for LLM proxy logging

Revision ID: 20260118_llm_requests
Revises: 20260113_proxy_agent_definitions
Create Date: 2026-01-18

Creates the llm_requests table for the LLM proxy to log all API requests.
This replaces the events table for LLM call tracking with a simpler,
more focused schema.

The proxy logs:
- Full request/response bodies (JSONB)
- Token counts (extracted from response for easy querying)
- Latency
- Model used

Cost computation happens via a view joining with model_metadata.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "20260118_llm_requests"
down_revision = "20260113_proxy_agent_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create llm_requests table and related infrastructure."""
    # Create the table
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
        sa.Column(
            "created_at",
            sa.TIMESTAMP,
            nullable=False,
            server_default=sa.func.now(),
        ),
        comment="LLM API requests logged by the proxy. Replaces events table for LLM tracking.",
    )

    # Index for querying by agent run (most common access pattern)
    op.create_index(
        "ix_llm_requests_agent_run_created",
        "llm_requests",
        ["agent_run_id", "created_at"],
    )

    # Enable RLS
    op.execute("ALTER TABLE llm_requests ENABLE ROW LEVEL SECURITY")

    # Create function to check if ancestor_id is in the parent chain of descendant_id
    # Returns true if ancestor_id = descendant_id OR ancestor_id is a parent/grandparent/etc
    op.execute("""
        CREATE OR REPLACE FUNCTION is_agent_ancestor(ancestor_id UUID, descendant_id UUID)
        RETURNS BOOLEAN AS $$
        WITH RECURSIVE ancestors AS (
            -- Base case: the descendant itself
            SELECT agent_run_id, parent_agent_run_id
            FROM agent_runs
            WHERE agent_run_id = descendant_id

            UNION ALL

            -- Recursive case: walk up the parent chain
            SELECT ar.agent_run_id, ar.parent_agent_run_id
            FROM agent_runs ar
            JOIN ancestors a ON ar.agent_run_id = a.parent_agent_run_id
        )
        SELECT EXISTS (
            SELECT 1 FROM ancestors WHERE agent_run_id = ancestor_id
        );
        $$ LANGUAGE SQL STABLE SECURITY DEFINER;
    """)

    # RLS policy: agents can see their own requests and their subagents' requests
    # Admin (proxy) can see all
    op.execute("""
        CREATE POLICY llm_requests_select ON llm_requests FOR SELECT USING (
            current_agent_run_id() IS NULL  -- Admin can see all
            OR is_agent_ancestor(current_agent_run_id(), agent_run_id)  -- Agent sees own + descendants
        )
    """)

    # Only proxy (admin) can insert - it validates tokens before logging
    op.execute("""
        CREATE POLICY llm_requests_insert ON llm_requests FOR INSERT WITH CHECK (
            current_agent_run_id() IS NULL  -- Only admin/proxy can insert
        )
    """)

    # Create view for computing costs by joining with model_metadata
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
            -- Cost calculation using model_metadata pricing
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

    # Create view for aggregated costs per agent run
    op.execute("""
        CREATE OR REPLACE VIEW llm_run_costs AS
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


def downgrade() -> None:
    """Remove llm_requests table and related infrastructure."""
    op.execute("DROP VIEW IF EXISTS llm_run_costs")
    op.execute("DROP VIEW IF EXISTS llm_request_costs")
    op.execute("DROP POLICY IF EXISTS llm_requests_insert ON llm_requests")
    op.execute("DROP POLICY IF EXISTS llm_requests_select ON llm_requests")
    op.drop_table("llm_requests")
    op.execute("DROP FUNCTION IF EXISTS is_agent_ancestor(UUID, UUID)")
