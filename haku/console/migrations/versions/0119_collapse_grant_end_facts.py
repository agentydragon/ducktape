"""Collapse grant lifecycle actions into one optional-reason end fact.

Database grants used to distinguish an Agent ``release`` from an Operator ``revoke`` by
recording either ``released_at`` or ``revoked_at``. That distinction belongs to the caller's
authorization, not the grant's lifecycle: both make the grant unusable. Keep one ``ended_at``
fact and an optional audit reason instead.

Revision ID: 0119
Revises: 0118
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0119"
down_revision: str | None = "0118"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("kubernetes_grants", "http_grants")

_KUBERNETES_TRIGGER = "trg_haku_0113_kubernetes_grant_source_invariants"
_KUBERNETES_FUNCTION = "public.haku_0113_kubernetes_grant_source_invariants()"
_ACCESS_PROFILE_FUNCTION = "public.haku_0119_kubernetes_grant_source_invariants()"
_ACCESS_PROFILE_TRIGGER = "trg_haku_0119_kubernetes_grant_source_invariants"


def _principal_shape() -> str:
    return (
        "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
        "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL "
        "AND principal_access_profile_id IS NULL) OR "
        "(principal_kind = 'session' AND principal_agent_id IS NULL "
        "AND principal_session_id IS NOT NULL AND principal_access_profile_id IS NULL) OR "
        "(principal_kind = 'access_profile' AND principal_agent_id IS NULL "
        "AND principal_session_id IS NULL AND principal_access_profile_id IS NOT NULL)"
    )


def _old_principal_shape() -> str:
    return (
        "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
        "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL) OR "
        "(principal_kind = 'session' AND principal_agent_id IS NULL "
        "AND principal_session_id IS NOT NULL)"
    )


def _replace_kubernetes_source_trigger() -> None:
    op.execute(f"DROP TRIGGER {_KUBERNETES_TRIGGER} ON public.kubernetes_grants")
    op.execute(
        f"""
CREATE FUNCTION {_ACCESS_PROFILE_FUNCTION}
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        (NEW.principal_kind = 'agent'
         AND NEW.principal_agent_id IS NOT NULL
         AND NEW.principal_agent_id = NEW.owner_agent_id
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
            (NEW.principal_kind = 'agent' AND NEW.principal_agent_id = binding.agent_id)
            OR
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
             ))
            OR
            (NEW.principal_kind = 'access_profile'
             AND NEW.principal_access_profile_id = agent.access_profile_id)
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
    )
    op.execute(
        f"""
CREATE TRIGGER {_ACCESS_PROFILE_TRIGGER}
BEFORE INSERT OR UPDATE OF owner_agent_id, principal_kind, principal_agent_id,
                           principal_session_id, principal_access_profile_id, source_tool_call_id
ON public.kubernetes_grants
FOR EACH ROW EXECUTE FUNCTION {_ACCESS_PROFILE_FUNCTION}
"""
    )


def _end_shape() -> str:
    return "(ended_at IS NOT NULL OR end_reason IS NULL) AND (end_reason IS NULL OR btrim(end_reason) <> '')"


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("principal_access_profile_id", sa.Text(), nullable=True))
        op.drop_constraint(f"ck_{table}_principal_shape", table, type_="check")
        op.create_check_constraint(f"ck_{table}_principal_shape", table, _principal_shape())
        op.create_index(
            f"idx_{table}_access_profile_principal_expiry", table, ["principal_access_profile_id", "expires_at"]
        )
        op.add_column(table, sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(f"UPDATE {table} SET ended_at = COALESCE(released_at, revoked_at)")
        op.drop_constraint(f"ck_{table}_end_shape", table, type_="check")
        op.drop_column(table, "released_at")
        op.drop_column(table, "revoked_at")
        op.create_check_constraint(f"ck_{table}_end_shape", table, _end_shape())
    _replace_kubernetes_source_trigger()


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {_ACCESS_PROFILE_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_ACCESS_PROFILE_FUNCTION}")
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_end_shape", table, type_="check")
        op.add_column(table, sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(f"UPDATE {table} SET revoked_at = ended_at")
        op.drop_column(table, "ended_at")
        op.create_check_constraint(
            f"ck_{table}_end_shape",
            table,
            "num_nonnulls(released_at, revoked_at) <= 1 "
            "AND ((num_nonnulls(released_at, revoked_at) = 1) = (end_reason IS NOT NULL)) "
            "AND (end_reason IS NULL OR btrim(end_reason) <> '')",
        )
        op.drop_index(f"idx_{table}_access_profile_principal_expiry", table)
        op.drop_constraint(f"ck_{table}_principal_shape", table, type_="check")
        op.create_check_constraint(f"ck_{table}_principal_shape", table, _old_principal_shape())
        op.drop_column(table, "principal_access_profile_id")
    op.execute(
        f"""
CREATE TRIGGER {_KUBERNETES_TRIGGER}
BEFORE INSERT OR UPDATE OF owner_agent_id, principal_kind, principal_agent_id,
                           principal_session_id, source_tool_call_id
ON public.kubernetes_grants
FOR EACH ROW EXECUTE FUNCTION {_KUBERNETES_FUNCTION}
"""
    )
