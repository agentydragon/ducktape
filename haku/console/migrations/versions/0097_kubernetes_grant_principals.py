"""Separate Kubernetes grant ownership from principal applicability.

Existing grants remain Agent-principal grants. New relational principal columns support
an Agent or one exact Console session while the source-provenance trigger validates
Agent-facing creation against durable authenticated ToolCall state.

Also indexes the conversation item read's keyset branches: `read_items` pages
`conversation_item` and `conversation_turn` by the rows' defining stream positions — a tool
call's `opened_seq`, a completed item's `closed_seq`, an ended turn's `last_seq` — and these
partial indexes are what lets the planner serve each branch as a keyset walk rather than
filtering or sorting the conversation's whole row set per page.

Revision ID: 0097
Revises: 0096
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0097"
down_revision: str | None = "0099"
branch_labels: str | None = None
depends_on: str | None = None

_OLD_TRIGGER = "trg_haku_0091_kubernetes_grant_source_invariants"
_OLD_FUNCTION = "public.haku_0091_kubernetes_grant_source_invariants()"
_NEW_TRIGGER = "trg_haku_0097_kubernetes_grant_source_invariants"
_NEW_FUNCTION = "public.haku_0097_kubernetes_grant_source_invariants()"


def _create_new_source_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION public.haku_0097_kubernetes_grant_source_invariants()
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
                         AND source_session.status IN ('ready', 'responding')
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
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0097_kubernetes_grant_source_invariants
        BEFORE INSERT OR UPDATE OF owner_agent_id, principal_kind, principal_agent_id,
                                   principal_session_id, source_tool_call_id
        ON public.kubernetes_grants
        FOR EACH ROW EXECUTE FUNCTION public.haku_0097_kubernetes_grant_source_invariants()
        """
    )


def _create_old_source_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION public.haku_0091_kubernetes_grant_source_invariants()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM public.mcp_tool_calls AS call
                JOIN public.mcp_tool_call_principals AS principal
                  ON principal.tool_call_id = call.tool_call_id
                JOIN public.credential_bindings AS binding
                  ON binding.binding_id = principal.binding_id
                JOIN public.agents AS agent
                  ON agent.agent_id = binding.agent_id
                WHERE call.tool_call_id = NEW.source_tool_call_id
                  AND binding.agent_id = NEW.agent_id
                  AND agent.status NOT IN ('abandoned', 'deleted')
                  AND call.server_id = 'kubernetes'
                  AND call.tool_name = 'create_grant'
                  AND call.status IN ('running', 'ok')
                  AND call.approved_at IS NOT NULL
                  AND call.approval_policy_id IS NULL
            ) THEN
                RAISE EXCEPTION 'invalid Kubernetes grant source provenance'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'ck_kubernetes_grants_source_provenance';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0091_kubernetes_grant_source_invariants
        BEFORE INSERT OR UPDATE OF agent_id, source_tool_call_id ON public.kubernetes_grants
        FOR EACH ROW EXECUTE FUNCTION public.haku_0091_kubernetes_grant_source_invariants()
        """
    )


def upgrade() -> None:
    op.execute(f"DROP TRIGGER {_OLD_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_OLD_FUNCTION}")
    op.drop_index("idx_kubernetes_grants_agent_status_expiry", table_name="kubernetes_grants")
    op.drop_constraint("kubernetes_grants_agent_id_fkey", "kubernetes_grants", type_="foreignkey")
    op.alter_column(
        "kubernetes_grants",
        "agent_id",
        new_column_name="owner_agent_id",
        existing_type=UUID(as_uuid=True),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "kubernetes_grants_owner_agent_id_fkey",
        "kubernetes_grants",
        "agents",
        ["owner_agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )
    op.add_column("kubernetes_grants", sa.Column("principal_kind", sa.Text(), nullable=True))
    op.add_column("kubernetes_grants", sa.Column("principal_agent_id", UUID(as_uuid=True), nullable=True))
    op.add_column("kubernetes_grants", sa.Column("principal_session_id", UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE public.kubernetes_grants SET principal_kind = 'agent', principal_agent_id = owner_agent_id")
    op.alter_column("kubernetes_grants", "principal_kind", existing_type=sa.Text(), nullable=False)
    op.create_foreign_key(
        "kubernetes_grants_principal_agent_id_fkey",
        "kubernetes_grants",
        "agents",
        ["principal_agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "kubernetes_grants_principal_session_id_fkey",
        "kubernetes_grants",
        "sessions",
        ["principal_session_id"],
        ["session_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_kubernetes_grants_principal_shape",
        "kubernetes_grants",
        "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
        "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL) OR "
        "(principal_kind = 'session' AND principal_agent_id IS NULL "
        "AND principal_session_id IS NOT NULL)",
    )
    op.create_index(
        "idx_kubernetes_grants_owner_status_expiry", "kubernetes_grants", ["owner_agent_id", "status", "expires_at"]
    )
    op.create_index(
        "idx_kubernetes_grants_agent_principal_status_expiry",
        "kubernetes_grants",
        ["principal_agent_id", "status", "expires_at"],
    )
    op.create_index(
        "idx_kubernetes_grants_session_principal_status_expiry",
        "kubernetes_grants",
        ["principal_session_id", "status", "expires_at"],
    )
    _create_new_source_trigger()
    op.create_index(
        "idx_conversation_item_tool_call_opened",
        "conversation_item",
        ["conversation_id", "opened_seq"],
        postgresql_where=sa.text("item_type = 'tool_call'"),
    )
    op.create_index(
        "idx_conversation_item_completed",
        "conversation_item",
        ["conversation_id", "closed_seq"],
        postgresql_where=sa.text("status = 'complete'"),
    )
    op.create_index(
        "idx_conversation_turn_ended",
        "conversation_turn",
        ["conversation_id", "last_seq"],
        postgresql_where=sa.text("last_seq IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_conversation_turn_ended", table_name="conversation_turn")
    op.drop_index("idx_conversation_item_completed", table_name="conversation_item")
    op.drop_index("idx_conversation_item_tool_call_opened", table_name="conversation_item")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.kubernetes_grants
                WHERE principal_kind <> 'agent'
                   OR principal_agent_id IS DISTINCT FROM owner_agent_id
                   OR principal_session_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'cannot downgrade Kubernetes grants containing session or non-owner Agent principals';
            END IF;
        END;
        $$
        """
    )
    op.execute(f"DROP TRIGGER {_NEW_TRIGGER} ON public.kubernetes_grants")
    op.execute(f"DROP FUNCTION {_NEW_FUNCTION}")
    op.drop_index("idx_kubernetes_grants_session_principal_status_expiry", table_name="kubernetes_grants")
    op.drop_index("idx_kubernetes_grants_agent_principal_status_expiry", table_name="kubernetes_grants")
    op.drop_index("idx_kubernetes_grants_owner_status_expiry", table_name="kubernetes_grants")
    op.drop_constraint("ck_kubernetes_grants_principal_shape", "kubernetes_grants", type_="check")
    op.drop_constraint("kubernetes_grants_principal_session_id_fkey", "kubernetes_grants", type_="foreignkey")
    op.drop_constraint("kubernetes_grants_principal_agent_id_fkey", "kubernetes_grants", type_="foreignkey")
    op.drop_constraint("kubernetes_grants_owner_agent_id_fkey", "kubernetes_grants", type_="foreignkey")
    op.drop_column("kubernetes_grants", "principal_session_id")
    op.drop_column("kubernetes_grants", "principal_agent_id")
    op.drop_column("kubernetes_grants", "principal_kind")
    op.alter_column(
        "kubernetes_grants",
        "owner_agent_id",
        new_column_name="agent_id",
        existing_type=UUID(as_uuid=True),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "kubernetes_grants_agent_id_fkey",
        "kubernetes_grants",
        "agents",
        ["agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "idx_kubernetes_grants_agent_status_expiry", "kubernetes_grants", ["agent_id", "status", "expires_at"]
    )
    _create_old_source_trigger()
