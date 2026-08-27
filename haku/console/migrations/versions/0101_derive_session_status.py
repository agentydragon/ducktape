"""Derive `sessions.status` from stored facts instead of storing it.

The column summarized facts its writers also stored — allocation, attachment, the terminal error —
so it could disagree with them and every writer had to keep it true. The facts that had no column
of their own get one (`close_requested_at`, `ended_at`), backfilled from the column being removed
(`closing` → a close request, `closed`/`failed` → an end, `error` already carried), and
`database_schema.Session.status` computes the same vocabulary from the facts at read time. The
constraints stated in the status vocabulary become fact-shape constraints, and the partial lease
index's predicate stops listing statuses.

Conversation rows are deliberately *not* dropped here: `kubernetes_grants.principal_session_id`
references `sessions` with ON DELETE RESTRICT and grant rows are outside the conversation-drop
allowance, so the backfill above is both the simpler and the only safe path.

The 0097 grant-source trigger reads `sessions.status` inside PL/pgSQL, which no schema tool
validates against a dropped column, so it is replaced here with one asking the same liveness
question of the facts.

Revision ID: 0101
Revises: 0097
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0101"
down_revision: str | None = "0097"
branch_labels: str | None = None
depends_on: str | None = None

_DROPPED_CHECKS = ("ck_sessions_status", "ck_sessions_idle_bridge_token", "ck_sessions_idle_lease")
_FACT_CHECKS = (
    ("ck_sessions_error_ended", "error IS NULL OR ended_at IS NOT NULL"),
    ("ck_sessions_connected_allocated", "bridge_connected_at IS NULL OR bridge_token_fingerprint IS NOT NULL"),
    (
        "ck_sessions_allocation_lease",
        "ended_at IS NOT NULL OR close_requested_at IS NOT NULL "
        "OR ((bridge_token_fingerprint IS NULL) = (lease_expires_at IS NULL))",
    ),
    ("ck_sessions_claim_cleanup_ended", "claim_cleaned_at IS NULL OR ended_at IS NOT NULL"),
)

_OLD_TRIGGER = "trg_haku_0097_kubernetes_grant_source_invariants"
_OLD_FUNCTION = "public.haku_0097_kubernetes_grant_source_invariants()"
_NEW_TRIGGER = "trg_haku_0101_kubernetes_grant_source_invariants"
_NEW_FUNCTION = "public.haku_0101_kubernetes_grant_source_invariants()"

# 0097's source-provenance check verbatim, except that the source session's liveness is asked of
# the facts: live (not ended, no close requested) and runner-attached is what 'ready'/'responding'
# spelled.
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
          AND call.server_id = 'kubernetes'
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
                 AND {source_session_live}
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
    op.add_column("sessions", sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE sessions SET close_requested_at = updated_at WHERE status = 'closing'"))
    op.execute(sa.text("UPDATE sessions SET ended_at = updated_at WHERE status IN ('closed', 'failed')"))
    for name in _DROPPED_CHECKS:
        op.drop_constraint(name, "sessions", type_="check")
    op.drop_index("idx_sessions_expired_lease", table_name="sessions")
    op.drop_column("sessions", "status")
    for name, condition in _FACT_CHECKS:
        op.create_check_constraint(name, "sessions", condition)
    op.create_index(
        "idx_sessions_expired_lease",
        "sessions",
        ["lease_expires_at"],
        postgresql_where=sa.text(
            "ended_at IS NULL AND close_requested_at IS NULL AND bridge_token_fingerprint IS NOT NULL"
        ),
    )
    op.execute(f"DROP TRIGGER {_OLD_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_OLD_FUNCTION}")
    op.execute(
        _SOURCE_TRIGGER_BODY_TEMPLATE.format(
            function=_NEW_FUNCTION,
            source_session_live=(
                "source_session.ended_at IS NULL "
                "AND source_session.close_requested_at IS NULL "
                "AND source_session.bridge_connected_at IS NOT NULL"
            ),
        )
    )
    op.execute(_TRIGGER_TEMPLATE.format(trigger=_NEW_TRIGGER, function=_NEW_FUNCTION))


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {_NEW_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_NEW_FUNCTION}")
    op.drop_index("idx_sessions_expired_lease", table_name="sessions")
    for name, _ in _FACT_CHECKS:
        op.drop_constraint(name, "sessions", type_="check")
    op.add_column("sessions", sa.Column("status", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE sessions SET status = CASE
                WHEN ended_at IS NOT NULL AND error IS NOT NULL THEN 'failed'
                WHEN ended_at IS NOT NULL THEN 'closed'
                WHEN close_requested_at IS NOT NULL THEN 'closing'
                WHEN bridge_token_fingerprint IS NULL THEN 'idle'
                WHEN bridge_connected_at IS NULL THEN 'provisioning'
                ELSE 'ready'
            END
            """
        )
    )
    op.alter_column("sessions", "status", nullable=False)
    op.drop_column("sessions", "ended_at")
    op.drop_column("sessions", "close_requested_at")
    op.create_check_constraint(
        "ck_sessions_status",
        "sessions",
        "status IN ('idle','provisioning','ready','responding','closing','closed','failed')",
    )
    op.create_check_constraint(
        "ck_sessions_idle_bridge_token",
        "sessions",
        "(status = 'idle' AND bridge_token_fingerprint IS NULL) OR "
        "(status <> 'idle' AND (bridge_token_fingerprint IS NOT NULL OR status IN ('closing','closed','failed')))",
    )
    op.create_check_constraint(
        "ck_sessions_idle_lease",
        "sessions",
        "(status = 'idle' AND lease_expires_at IS NULL) OR "
        "(status <> 'idle' AND (lease_expires_at IS NOT NULL OR status IN ('closing','closed','failed')))",
    )
    op.create_index(
        "idx_sessions_expired_lease",
        "sessions",
        ["lease_expires_at"],
        postgresql_where=sa.text("status IN ('provisioning','ready','responding')"),
    )
    op.execute(
        _SOURCE_TRIGGER_BODY_TEMPLATE.format(
            function=_OLD_FUNCTION, source_session_live="source_session.status IN ('ready', 'responding')"
        )
    )
    op.execute(_TRIGGER_TEMPLATE.format(trigger=_OLD_TRIGGER, function=_OLD_FUNCTION))
