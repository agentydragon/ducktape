"""Allow Agent-requested grants to target any valid principal identity.

Revision ID: 0125
Revises: 0124
"""

from __future__ import annotations

from alembic import op

revision: str = "0125"
down_revision: str | None = "0124"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("kubernetes_grants", "http_grants")
_FUNCTION = "public.haku_0119_kubernetes_grant_source_invariants()"


def _principal_shape(*, agent_must_be_owner: bool) -> str:
    agent_identity = "AND principal_agent_id = owner_agent_id " if agent_must_be_owner else ""
    return (
        "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
        f"{agent_identity}AND principal_session_id IS NULL "
        "AND principal_access_profile_id IS NULL) OR "
        "(principal_kind = 'session' AND principal_agent_id IS NULL "
        "AND principal_session_id IS NOT NULL AND principal_access_profile_id IS NULL) OR "
        "(principal_kind = 'access_profile' AND principal_agent_id IS NULL "
        "AND principal_session_id IS NULL AND principal_access_profile_id IS NOT NULL)"
    )


def _source_function(*, allow_any_principal: bool) -> str:
    if allow_any_principal:
        agent_shape = "AND NEW.principal_agent_id IS NOT NULL"
        agent_provenance = """
            (NEW.principal_kind = 'agent'
             AND EXISTS (
               SELECT 1
               FROM public.agents AS target_agent
               WHERE target_agent.agent_id = NEW.principal_agent_id
                 AND target_agent.status NOT IN ('abandoned', 'deleted')
             ))"""
        session_provenance = """
            (NEW.principal_kind = 'session'
             AND EXISTS (
               SELECT 1
               FROM public.sessions AS target_session
               WHERE target_session.session_id = NEW.principal_session_id
                 AND target_session.agent_binding_id IS NOT NULL
                 AND target_session.ended_at IS NULL
                 AND target_session.close_requested_at IS NULL
                 AND target_session.bridge_connected_at IS NOT NULL
                 AND target_session.lease_expires_at > statement_timestamp()
             ))"""
        profile_provenance = """
            (NEW.principal_kind = 'access_profile'
             AND NEW.principal_access_profile_id IS NOT NULL)"""
    else:
        agent_shape = "AND NEW.principal_agent_id = NEW.owner_agent_id"
        agent_provenance = """
            (NEW.principal_kind = 'agent' AND NEW.principal_agent_id = binding.agent_id)"""
        session_provenance = """
            (NEW.principal_kind = 'session'
             AND request_principal.session_id IS NOT NULL
             AND NEW.principal_session_id = request_principal.session_id
             AND EXISTS (
               SELECT 1
               FROM public.sessions AS source_session
               WHERE source_session.session_id = request_principal.session_id
                 AND source_session.agent_binding_id = request_principal.binding_id
                 AND source_session.ended_at IS NULL
                 AND source_session.close_requested_at IS NULL
                 AND source_session.bridge_connected_at IS NOT NULL
                 AND source_session.lease_expires_at > statement_timestamp()
             ))"""
        profile_provenance = """
            (NEW.principal_kind = 'access_profile'
             AND NEW.principal_access_profile_id = agent.access_profile_id)"""
    return f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        (NEW.principal_kind = 'agent'
         {agent_shape}
         AND NEW.principal_session_id IS NULL
         AND NEW.principal_access_profile_id IS NULL)
        OR
        (NEW.principal_kind = 'session'
         AND NEW.principal_agent_id IS NULL
         AND NEW.principal_session_id IS NOT NULL
         AND NEW.principal_access_profile_id IS NULL)
        OR
        (NEW.principal_kind = 'access_profile'
         AND NEW.principal_agent_id IS NULL
         AND NEW.principal_session_id IS NULL
         AND NEW.principal_access_profile_id IS NOT NULL)
    ) THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_tool_calls AS call
        JOIN public.mcp_tool_call_principals AS request_principal
          ON request_principal.tool_call_id = call.tool_call_id
        JOIN public.credential_bindings AS binding
          ON binding.binding_id = request_principal.binding_id
        JOIN public.agents AS agent
          ON agent.agent_id = binding.agent_id
        WHERE call.tool_call_id = NEW.source_tool_call_id
          AND binding.agent_id = NEW.owner_agent_id
          AND agent.status NOT IN ('abandoned', 'deleted')
          AND call.server_id IN ('kubernetes', 'grants')
          AND call.tool_name = 'create_grant'
          AND call.status IN ('running', 'ok')
          AND call.approved_at IS NOT NULL
          AND call.approval_policy_id IS NULL
          AND (
            {agent_provenance}
            OR
            {session_provenance}
            OR
            {profile_provenance}
          )
    ) THEN
        RAISE EXCEPTION 'invalid Kubernetes grant source provenance or principal'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_kubernetes_grants_source_provenance';
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_principal_shape", table, type_="check")
        op.create_check_constraint(f"ck_{table}_principal_shape", table, _principal_shape(agent_must_be_owner=False))
    op.execute(_source_function(allow_any_principal=True))


def downgrade() -> None:
    op.execute(_source_function(allow_any_principal=False))
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_principal_shape", table, type_="check")
        op.create_check_constraint(f"ck_{table}_principal_shape", table, _principal_shape(agent_must_be_owner=True))
