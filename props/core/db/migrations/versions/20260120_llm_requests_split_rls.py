"""Add split-based RLS policy for llm_requests.

Revision ID: 20260120_llm_requests_split_rls
Revises: 20260120_add_resource_limits_and_lifecycle
Create Date: 2026-01-20

Adds RLS policy allowing prompt_optimizer users to access TRAIN split
LLM requests (similar to the removed events table policy).
"""

from alembic import op

revision = "20260120_llm_requests_split_rls"
down_revision = "20260120_add_resource_limits_and_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the existing SELECT policy and recreate with split-based access
    op.execute("DROP POLICY IF EXISTS llm_requests_select ON llm_requests")

    # Recreate with split-based access for prompt_optimizer
    op.execute("""
        CREATE POLICY llm_requests_select ON llm_requests FOR SELECT USING (
            -- Admin can see all
            current_agent_run_id() IS NULL
            -- Agent sees own + descendants
            OR is_agent_ancestor(current_agent_run_id(), agent_run_id)
            -- Prompt optimizer can see TRAIN split requests
            OR (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(agent_run_id))
            -- Improvement agent can see allowed agent runs' requests
            OR (current_agent_type() = 'improvement'
                AND agent_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)


def downgrade() -> None:
    # Restore original policy without split-based access
    op.execute("DROP POLICY IF EXISTS llm_requests_select ON llm_requests")

    op.execute("""
        CREATE POLICY llm_requests_select ON llm_requests FOR SELECT USING (
            current_agent_run_id() IS NULL
            OR is_agent_ancestor(current_agent_run_id(), agent_run_id)
        )
    """)
