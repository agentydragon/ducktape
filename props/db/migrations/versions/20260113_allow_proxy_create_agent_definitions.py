"""Allow proxy to create agent_definitions

Revision ID: 20260113_proxy_agent_definitions
Revises: 20251228000000
Create Date: 2026-01-13

Updates RLS policy for agent_definitions to allow the proxy (running as admin)
to create agent_definitions rows on behalf of PO/PI agents.

The proxy intercepts manifest pushes and automatically writes agent_definitions
rows. It connects as admin, not as an agent, so current_agent_run_id() returns NULL.

Policy change:
- Allow admin users to insert with any created_by_agent_run_id (for proxy)
- Disallow agent users from inserting directly (only proxy should create these)
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260113_proxy_agent_definitions"
down_revision = "20251228000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Update agent_definitions RLS policy to allow proxy writes."""
    # Drop old policy that only allowed agents to insert their own records
    op.execute("DROP POLICY IF EXISTS agent_definitions_insert ON agent_definitions")

    # New policy: allow admin (proxy) to insert, disallow agents
    # Admin users have current_agent_run_id() = NULL
    # Agents have current_agent_run_id() = their run_id
    op.execute("""
        CREATE POLICY agent_definitions_insert ON agent_definitions FOR INSERT WITH CHECK (
            current_agent_run_id() IS NULL  -- Only admin/proxy can insert
        )
    """)

    op.execute("""
        COMMENT ON POLICY agent_definitions_insert ON agent_definitions IS
        'Only admin/proxy can insert agent_definitions. Agents cannot insert directly.
The proxy intercepts manifest pushes and creates agent_definitions rows automatically.'
    """)


def downgrade() -> None:
    """Restore original agent_definitions RLS policy."""
    op.execute("DROP POLICY IF EXISTS agent_definitions_insert ON agent_definitions")

    # Restore original policy
    op.execute("""
        CREATE POLICY agent_definitions_insert ON agent_definitions FOR INSERT WITH CHECK (
            created_by_agent_run_id = current_agent_run_id()
        )
    """)
