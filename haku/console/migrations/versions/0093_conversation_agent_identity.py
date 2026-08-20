"""Cut over conversations to immutable Agent/profile identity.

The v3 frame cutover already removed the old session-derived projection.  This revision repeats
only that small allowlist so a database upgraded through the 0090 cutover remains safe, then adds
identity columns.  Existing conversations remain readable as rows/Matrix attachments but have
NULL identity and are deliberately rejected by the launch service until recreated.

Revision ID: 0093
Revises: 0092
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("conversation", sa.Column("access_profile_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_conversation_agent_id", "conversation", "agents", ["agent_id"], ["agent_id"], ondelete="RESTRICT"
    )
    op.create_check_constraint(
        "ck_conversation_access_profile_id_nonempty",
        "conversation",
        "access_profile_id IS NULL OR btrim(access_profile_id) <> ''",
    )
    op.create_check_constraint(
        "ck_conversation_agent_profile_pair", "conversation", "(agent_id IS NULL) = (access_profile_id IS NULL)"
    )

    # Identity cutover is intentionally narrow and irreversible: these are all rows derived from a
    # runner/session and can be regenerated. Operators, Agents, credentials, approvals, provider
    # connections, conversations and Matrix attachment addresses are not touched.
    op.execute("DELETE FROM conversation_prompt")
    op.execute("DELETE FROM conversation_event")
    op.execute("DELETE FROM conversation_item")
    op.execute("DELETE FROM conversation_turn")
    op.execute("DELETE FROM session_frames")
    op.execute("DELETE FROM sessions")

    op.add_column("sessions", sa.Column("agent_binding_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_sessions_agent_binding_id",
        "sessions",
        "credential_bindings",
        ["agent_binding_id"],
        ["binding_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_sessions_session_agent_binding", "sessions", ["session_id", "agent_binding_id"])
    # A sandbox presents this one per-session bearer both to the runner websocket and to /mcp.
    # Uniqueness makes the credential an unambiguous session selector; NULL remains valid for any
    # number of idle sessions that have not allocated a sandbox.
    op.create_unique_constraint("uq_sessions_bridge_token_fingerprint", "sessions", ["bridge_token_fingerprint"])
    op.add_column("mcp_tool_call_principals", sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_mcp_tool_call_principals_session_binding",
        "mcp_tool_call_principals",
        "sessions",
        ["session_id", "binding_id"],
        ["session_id", "agent_binding_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_mcp_tool_call_principals_session_agent",
        "mcp_tool_call_principals",
        "session_id IS NULL OR binding_id IS NOT NULL",
    )
    op.create_index("idx_mcp_tool_call_principals_session_id", "mcp_tool_call_principals", ["session_id"], unique=False)

    # Where the old thread belongs to an Operator who still has exactly one active profiled Agent,
    # retain its Matrix address and make the cutover usable without guessing from a caller.  Rows
    # with no such durable owner remain NULL and are fail-closed by the launch service.
    op.execute(
        sa.text(
            """
            WITH eligible AS (
                SELECT
                    a.owner_operator_id,
                    (array_agg(a.agent_id ORDER BY a.agent_id))[1] AS agent_id,
                    (array_agg(a.access_profile_id ORDER BY a.agent_id))[1] AS access_profile_id
                FROM agents AS a
                JOIN operators AS o ON o.operator_id = a.owner_operator_id
                JOIN credential_bindings AS cb ON cb.agent_id = a.agent_id
                WHERE o.status = 'active'
                  AND a.status = 'active'
                  AND a.access_profile_id IS NOT NULL
                  AND cb.status = 'active'
                GROUP BY a.owner_operator_id
                HAVING count(DISTINCT a.agent_id) = 1
            )
            UPDATE conversation AS c
            SET agent_id = eligible.agent_id,
                access_profile_id = eligible.access_profile_id
            FROM eligible
            WHERE c.operator_id = eligible.owner_operator_id
              AND c.agent_id IS NULL
            """
        )
    )

    # The application is the only supported writer of identity.  A database trigger prevents an
    # accidental profile/Agent retarget, while allowing legacy NULL rows to be repaired only by a
    # future explicit data migration before the trigger is installed.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_conversation_identity_update() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.agent_id IS DISTINCT FROM OLD.agent_id
                   OR NEW.access_profile_id IS DISTINCT FROM OLD.access_profile_id
                   OR NEW.runtime_kind IS DISTINCT FROM OLD.runtime_kind THEN
                    RAISE EXCEPTION 'conversation identity is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER conversation_identity_immutable
            BEFORE UPDATE OF agent_id, access_profile_id, runtime_kind ON conversation
            FOR EACH ROW EXECUTE FUNCTION prevent_conversation_identity_update()
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("conversation Agent identity cutover is intentionally irreversible")
