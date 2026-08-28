"""Accept the shared `grants` server as a Kubernetes grant's source ToolCall (#4918).

Grant creation consolidated from the per-domain `kubernetes`/`http_grants` in-process servers onto
one `grants` server. The Kubernetes grant source-provenance trigger (last defined in 0101) pinned
`call.server_id = 'kubernetes'`; a grant minted by the new server is sourced from a `server_id =
'grants'` ToolCall, so the pin must admit it.

Roll-safe expand, not a swap: the console rolls with overlapping replicas, and a pre-cutover replica
still mints Kubernetes grants from a `'kubernetes'` create_grant call while a post-cutover replica
mints them from `'grants'`. The trigger therefore accepts both `server_id` values so neither
replica's insert is rejected mid-roll. Once the cutover release is fully rolled out, a later
migration may contract the set to `'grants'` alone. Stored audit rows keep their historical
`server_id` and are never re-inserted, so this governs only newly minted grants. HTTP grants carry
no `server_id` provenance pin (their trigger never constrained it), so only the Kubernetes trigger
changes here.

Revision ID: 0113
Revises: 0117
"""

from __future__ import annotations

from alembic import op

revision: str = "0113"
down_revision: str | None = "0117"
branch_labels: str | None = None
depends_on: str | None = None

_OLD_TRIGGER = "trg_haku_0101_kubernetes_grant_source_invariants"
_OLD_FUNCTION = "public.haku_0101_kubernetes_grant_source_invariants()"
_NEW_TRIGGER = "trg_haku_0113_kubernetes_grant_source_invariants"
_NEW_FUNCTION = "public.haku_0113_kubernetes_grant_source_invariants()"

# 0101's facts-based source-provenance body verbatim, except that the source ToolCall's server may be
# the pre-cutover `kubernetes` server or the consolidated `grants` server ({server_ids}).
_SOURCE_TRIGGER_BODY_TEMPLATE = """
CREATE FUNCTION {function}
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Shape is enforced independently by ck_kubernetes_grants_principal_shape.
    -- Let that constraint report malformed relational combinations instead of
    -- misclassifying them as source-provenance failures in this BEFORE trigger.
    IF NOT (
        (NEW.principal_kind = 'agent'
         AND NEW.principal_agent_id IS NOT NULL
         AND NEW.principal_agent_id = NEW.owner_agent_id
         AND NEW.principal_session_id IS NULL)
        OR
        (NEW.principal_kind = 'session'
         AND NEW.principal_agent_id IS NULL
         AND NEW.principal_session_id IS NOT NULL)
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
          AND call.server_id IN ({server_ids})
          AND call.tool_name = 'create_grant'
          AND call.status IN ('running', 'ok')
          AND call.approved_at IS NOT NULL
          AND call.approval_policy_id IS NULL
          AND (
            (NEW.principal_kind = 'agent'
             AND NEW.principal_agent_id = binding.agent_id)
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
          )
    ) THEN
        RAISE EXCEPTION 'invalid Kubernetes grant source provenance or principal'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_kubernetes_grants_source_provenance';
    END IF;
    RETURN NEW;
END;
$$
"""

_TRIGGER_TEMPLATE = """
CREATE TRIGGER {trigger}
BEFORE INSERT OR UPDATE OF owner_agent_id, principal_kind, principal_agent_id,
                           principal_session_id, source_tool_call_id
ON public.kubernetes_grants
FOR EACH ROW EXECUTE FUNCTION {function}
"""


def upgrade() -> None:
    op.execute(f"DROP TRIGGER {_OLD_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_OLD_FUNCTION}")
    op.execute(_SOURCE_TRIGGER_BODY_TEMPLATE.format(function=_NEW_FUNCTION, server_ids="'kubernetes', 'grants'"))
    op.execute(_TRIGGER_TEMPLATE.format(trigger=_NEW_TRIGGER, function=_NEW_FUNCTION))


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {_NEW_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_NEW_FUNCTION}")
    op.execute(_SOURCE_TRIGGER_BODY_TEMPLATE.format(function=_OLD_FUNCTION, server_ids="'kubernetes'"))
    op.execute(_TRIGGER_TEMPLATE.format(trigger=_OLD_TRIGGER, function=_OLD_FUNCTION))
