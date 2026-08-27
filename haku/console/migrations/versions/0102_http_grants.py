"""Persist principal-scoped temporary HTTP egress grants and their provenance.

A sibling of the Kubernetes grant domain: same ownership, principal, provenance, and lifecycle
columns, but the capability is one exact canonical public origin held in three relational columns
``(scheme, host, port)`` rather than an open JSONB vocabulary. The domain canonicalizes the host
to its lowercase IDNA A-label before insertion; PostgreSQL enforces that canonical shape plus the
lifecycle and manual-approval source-provenance invariants here.

Revision ID: 0102
Revises: 0097
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0102"
down_revision: str | None = "0101"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "http_grants",
        sa.Column("grant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("principal_kind", sa.Text(), nullable=False),
        sa.Column("principal_agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("principal_session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_tool_call_id", sa.Text(), nullable=False),
        sa.Column("scheme", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("grant_id", name="http_grants_pkey"),
        sa.ForeignKeyConstraint(
            ["owner_agent_id"], ["agents.agent_id"], name="http_grants_owner_agent_id_fkey", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["principal_agent_id"], ["agents.agent_id"], name="http_grants_principal_agent_id_fkey", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["principal_session_id"],
            ["sessions.session_id"],
            name="http_grants_principal_session_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_tool_call_id"],
            ["mcp_tool_calls.tool_call_id"],
            name="http_grants_source_tool_call_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("btrim(source_tool_call_id) <> ''", name="ck_http_grants_source_tool_call_nonempty"),
        sa.CheckConstraint(
            "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
            "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL) OR "
            "(principal_kind = 'session' AND principal_agent_id IS NULL "
            "AND principal_session_id IS NOT NULL)",
            name="ck_http_grants_principal_shape",
        ),
        sa.CheckConstraint(
            "scheme IN ('http', 'https') AND port >= 1 AND port <= 65535 "
            "AND char_length(host) <= 253 "
            "AND host ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'",
            name="ck_http_grants_origin_shape",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_http_grants_expiration_after_creation"),
        sa.CheckConstraint(
            "(status IN ('active','released','revoked','expired')) AND "
            "((status = 'active' AND ended_at IS NULL AND end_reason IS NULL) OR "
            "(status IN ('released', 'revoked', 'expired') AND ended_at IS NOT NULL "
            "AND end_reason IS NOT NULL AND btrim(end_reason) <> ''))",
            name="ck_http_grants_status_shape",
        ),
    )
    op.create_index("idx_http_grants_source_tool_call", "http_grants", ["source_tool_call_id"])
    op.create_index("idx_http_grants_owner_status_expiry", "http_grants", ["owner_agent_id", "status", "expires_at"])
    op.create_index(
        "idx_http_grants_agent_principal_status_expiry", "http_grants", ["principal_agent_id", "status", "expires_at"]
    )
    op.create_index(
        "idx_http_grants_session_principal_status_expiry",
        "http_grants",
        ["principal_session_id", "status", "expires_at"],
    )
    op.execute(
        """
        CREATE FUNCTION public.haku_0102_http_grant_source_invariants()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Shape is enforced independently by ck_http_grants_principal_shape. Let that
            -- constraint report malformed relational combinations instead of misclassifying
            -- them as source-provenance failures in this BEFORE trigger.
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
                  AND call.server_id = 'http'
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
                RAISE EXCEPTION 'invalid HTTP grant source provenance or principal'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'ck_http_grants_source_provenance';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_haku_0102_http_grant_source_invariants
        BEFORE INSERT OR UPDATE OF owner_agent_id, principal_kind, principal_agent_id,
                                   principal_session_id, source_tool_call_id
        ON public.http_grants
        FOR EACH ROW EXECUTE FUNCTION public.haku_0102_http_grant_source_invariants()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_haku_0102_http_grant_source_invariants ON public.http_grants")
    op.execute("DROP FUNCTION public.haku_0102_http_grant_source_invariants()")
    op.drop_index("idx_http_grants_session_principal_status_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_agent_principal_status_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_owner_status_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_source_tool_call", table_name="http_grants")
    op.drop_table("http_grants")
