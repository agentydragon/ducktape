"""Drop the hosted-agent runtime tables: conversation, sessions, and the Matrix channel.

The console no longer runs Agents in its own sandboxes or hosts a conversation transcript — every
sandbox, harness, channel, and recall-index surface that read or wrote these tables is gone from
the application. What is left is dead schema.

**Drops conversation-scoped rows outright**, per <../../AGENTS.md> § Conversation data may be
dropped: `conversation` is the root and every table here cascades from it (directly, or through
`channel_attachment`/`submitted_prompt`), so nothing here is exempt from that allowance. Nothing
audit-scoped is touched: `mcp_tool_calls`/`mcp_tool_call_principals` keep every row, only the now
unenforceable foreign key from `mcp_tool_call_principals.session_id` to the dropped `sessions` table
is dropped — the column itself stays as an inert historical field.

**A grant may no longer name a live session as its principal** (`SessionGrantPrincipal` is gone):
`kubernetes_grants`/`http_grants` drop their `principal_session_id` column, its FK into `sessions`,
and the `session` arm of both the principal-shape CHECK and the source-provenance trigger function.
A grant a hosted session held (`principal_kind = 'session'`) has no equivalent principal to
reproject onto, so those rows are deleted outright — the same conversation-scoped allowance covers
this, since a session-scoped grant's authority never outlived the hosted session that requested it.

Revision ID: 0129
Revises: 0128
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0129"
down_revision: str | None = "0128"
branch_labels: str | None = None
depends_on: str | None = None

_GRANT_TABLES = ("kubernetes_grants", "http_grants")

# Only `kubernetes_grants` carries the DB-level source-provenance trigger (defense in depth
# alongside the Python check); `http_grants` relies on the Python check alone (0102 created no
# trigger for it).
_KUBERNETES_TRIGGER = "trg_haku_0119_kubernetes_grant_source_invariants"
_KUBERNETES_TRIGGER_COLUMNS_WITHOUT_SESSION = (
    "owner_agent_id, principal_kind, principal_agent_id, principal_access_profile_id, source_tool_call_id"
)
_KUBERNETES_TRIGGER_COLUMNS_WITH_SESSION = (
    "owner_agent_id, principal_kind, principal_agent_id, "
    "principal_session_id, principal_access_profile_id, source_tool_call_id"
)

_PRINCIPAL_SHAPE_WITHOUT_SESSION = (
    "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL AND principal_access_profile_id IS NULL) OR "
    "(principal_kind = 'access_profile' AND principal_agent_id IS NULL AND principal_access_profile_id IS NOT NULL)"
)

_PRINCIPAL_SHAPE_WITH_SESSION = (
    "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
    "AND principal_session_id IS NULL AND principal_access_profile_id IS NULL) OR "
    "(principal_kind = 'session' AND principal_agent_id IS NULL "
    "AND principal_session_id IS NOT NULL AND principal_access_profile_id IS NULL) OR "
    "(principal_kind = 'access_profile' AND principal_agent_id IS NULL "
    "AND principal_session_id IS NULL AND principal_access_profile_id IS NOT NULL)"
)

_SOURCE_FUNCTION = "public.haku_0119_kubernetes_grant_source_invariants()"

# Mirrors 0125's `_source_function(allow_any_principal=True)`, minus the `session` provenance arm
# a hosted-session principal used: unreachable now that no row can carry `principal_kind = 'session'`.
_SOURCE_FUNCTION_WITHOUT_SESSION = f"""
CREATE OR REPLACE FUNCTION {_SOURCE_FUNCTION}
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        (NEW.principal_kind = 'agent'
         AND NEW.principal_access_profile_id IS NULL)
        OR
        (NEW.principal_kind = 'access_profile'
         AND NEW.principal_agent_id IS NULL
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
            (NEW.principal_kind = 'agent'
             AND EXISTS (
               SELECT 1
               FROM public.agents AS target_agent
               WHERE target_agent.agent_id = NEW.principal_agent_id
                 AND target_agent.status NOT IN ('abandoned', 'deleted')
             ))
            OR
            (NEW.principal_kind = 'access_profile'
             AND NEW.principal_access_profile_id IS NOT NULL)
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

# The pre-0129 function (0125's `_source_function(allow_any_principal=True)`), restored on downgrade.
_SOURCE_FUNCTION_WITH_SESSION = f"""
CREATE OR REPLACE FUNCTION {_SOURCE_FUNCTION}
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        (NEW.principal_kind = 'agent'
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
            (NEW.principal_kind = 'agent'
             AND EXISTS (
               SELECT 1
               FROM public.agents AS target_agent
               WHERE target_agent.agent_id = NEW.principal_agent_id
                 AND target_agent.status NOT IN ('abandoned', 'deleted')
             ))
            OR
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
             ))
            OR
            (NEW.principal_kind = 'access_profile'
             AND NEW.principal_access_profile_id IS NOT NULL)
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
    # `trg_haku_0119_kubernetes_grant_source_invariants` was created `UPDATE OF ... principal_session_id
    # ...`, which is a real Postgres dependency on that column — drop and recreate it with a narrower
    # column list before the column can go. `CREATE OR REPLACE FUNCTION` alone does not touch this: a
    # plpgsql function body is opaque to the dependency tracker, so only the trigger's own column list
    # blocked the later `DROP COLUMN`.
    op.execute(f"DROP TRIGGER {_KUBERNETES_TRIGGER} ON public.kubernetes_grants")
    op.execute(_SOURCE_FUNCTION_WITHOUT_SESSION)
    op.execute(
        f"""
CREATE TRIGGER {_KUBERNETES_TRIGGER}
BEFORE INSERT OR UPDATE OF {_KUBERNETES_TRIGGER_COLUMNS_WITHOUT_SESSION}
ON public.kubernetes_grants
FOR EACH ROW EXECUTE FUNCTION {_SOURCE_FUNCTION}
"""
    )

    for table in _GRANT_TABLES:
        op.execute(sa.text(f"DELETE FROM {table} WHERE principal_kind = 'session'"))
        op.drop_constraint(f"{table}_principal_session_id_fkey", table, type_="foreignkey")
        op.drop_constraint(f"ck_{table}_principal_shape", table, type_="check")
        op.create_check_constraint(f"ck_{table}_principal_shape", table, _PRINCIPAL_SHAPE_WITHOUT_SESSION)
        op.drop_column(table, "principal_session_id")

    op.drop_constraint("fk_mcp_tool_call_principals_session_binding", "mcp_tool_call_principals", type_="foreignkey")

    op.drop_table("matrix_ingress_event")
    op.drop_table("matrix_outbox")
    op.drop_table("matrix_room_copy")
    op.drop_table("matrix_revision")
    op.drop_table("matrix_sync_watermark")
    op.drop_table("matrix_access_token")

    op.drop_table("session_frames")
    op.drop_table("submitted_prompt")
    op.drop_table("conversation_prompt")
    op.drop_table("conversation_event")
    op.drop_table("conversation_item")
    op.drop_table("conversation_turn")
    op.drop_table("channel_cursor")
    op.drop_table("channel_attachment")
    op.drop_table("sessions")
    op.drop_table("conversation")


def downgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("access_profile_id", sa.Text(), nullable=True),
        sa.Column("harness_kind", sa.Text(), nullable=False),
        sa.Column("next_event_seq", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id", name="conversation_pkey"),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="conversation_operator_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.agent_id"], name="conversation_agent_id_fkey", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "access_profile_id IS NULL OR btrim(access_profile_id) <> ''",
            name="ck_conversation_access_profile_id_nonempty",
        ),
        sa.CheckConstraint(
            "(agent_id IS NULL) = (access_profile_id IS NULL)", name="ck_conversation_agent_profile_pair"
        ),
        sa.CheckConstraint("harness_kind IN ('claude_code', 'codex_app_server')", name="ck_conversation_harness_kind"),
        sa.CheckConstraint("next_event_seq > 0", name="ck_conversation_next_event_seq"),
    )
    op.create_index("idx_conversation_operator", "conversation", ["operator_id", "created_at"])

    op.create_table(
        "sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_binding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bridge_token_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("bridge_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_token_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("runner_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projected_frame_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("acked_batch_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abort_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_holder", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name="sessions_pkey"),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="sessions_operator_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="sessions_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_binding_id"],
            ["credential_bindings.binding_id"],
            name="sessions_agent_binding_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("bridge_token_fingerprint", name="uq_sessions_bridge_token_fingerprint"),
        sa.UniqueConstraint("session_token_fingerprint", name="uq_sessions_session_token_fingerprint"),
        sa.UniqueConstraint("session_id", "agent_binding_id", name="uq_sessions_session_agent_binding"),
        sa.CheckConstraint("error IS NULL OR ended_at IS NOT NULL", name="ck_sessions_error_ended"),
        sa.CheckConstraint(
            "bridge_connected_at IS NULL OR bridge_token_fingerprint IS NOT NULL",
            name="ck_sessions_connected_allocated",
        ),
        sa.CheckConstraint(
            "ended_at IS NOT NULL OR close_requested_at IS NOT NULL "
            "OR ((bridge_token_fingerprint IS NULL) = (lease_expires_at IS NULL))",
            name="ck_sessions_allocation_lease",
        ),
        sa.CheckConstraint("claim_cleaned_at IS NULL OR ended_at IS NOT NULL", name="ck_sessions_claim_cleanup_ended"),
    )
    op.create_index("idx_sessions_operator", "sessions", ["operator_id", "created_at"])
    op.create_index("idx_sessions_conversation", "sessions", ["conversation_id", "created_at"])
    op.create_index(
        "idx_sessions_expired_lease",
        "sessions",
        ["lease_expires_at"],
        postgresql_where=sa.text(
            "ended_at IS NULL AND close_requested_at IS NULL AND bridge_token_fingerprint IS NOT NULL"
        ),
    )

    op.create_table(
        "channel_attachment",
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detached_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("attachment_id", name="channel_attachment_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="channel_attachment_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("surface IN ('matrix')", name="ck_channel_attachment_surface"),
        sa.CheckConstraint("btrim(address) <> ''", name="ck_channel_attachment_address_nonempty"),
        sa.CheckConstraint(
            "detached_at IS NULL OR detached_at >= attached_at", name="ck_channel_attachment_detach_after_attach"
        ),
    )
    op.create_index(
        "uq_channel_attachment_live_address",
        "channel_attachment",
        ["surface", "address"],
        unique=True,
        postgresql_where=sa.text("detached_at IS NULL"),
    )
    op.create_index("idx_channel_attachment_conversation", "channel_attachment", ["conversation_id", "attached_at"])

    op.create_table(
        "channel_cursor",
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("attachment_id", name="channel_cursor_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["channel_attachment.attachment_id"],
            name="channel_cursor_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("event_seq >= 0", name="ck_channel_cursor_event_seq"),
    )

    op.create_table(
        "conversation_turn",
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=True),
        sa.Column("runner_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("last_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("turn_id", name="conversation_turn_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_turn_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="conversation_turn_session_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','aborted','failed')", name="ck_conversation_turn_outcome"
        ),
        sa.CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ck_conversation_turn_ended"),
        sa.CheckConstraint(
            "(failure IS NULL) = (outcome IS DISTINCT FROM 'failed')", name="ck_conversation_turn_failure"
        ),
        sa.CheckConstraint("(ended_at IS NULL) = (last_seq IS NULL)", name="ck_conversation_turn_last_seq"),
        sa.CheckConstraint("last_seq IS NULL OR last_seq >= first_seq", name="ck_conversation_turn_seq_order"),
    )
    op.create_index(
        "uq_conversation_turn_open",
        "conversation_turn",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "uq_conversation_turn_runner",
        "conversation_turn",
        ["session_id", "runner_turn_id"],
        unique=True,
        postgresql_where=sa.text("runner_turn_id IS NOT NULL"),
    )
    op.create_index("idx_conversation_turn_conversation", "conversation_turn", ["conversation_id", "first_seq"])
    op.create_index("idx_conversation_turn_session", "conversation_turn", ["session_id", "first_seq"])
    op.create_index(
        "idx_conversation_turn_ended",
        "conversation_turn",
        ["conversation_id", "last_seq"],
        postgresql_where=sa.text("last_seq IS NOT NULL"),
    )

    op.create_table(
        "conversation_item",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("opened_seq", sa.BigInteger(), nullable=False),
        sa.Column("closed_seq", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("backend_item_id", sa.Text(), nullable=True),
        sa.Column("runner_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("arguments", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("structured", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("disclosure", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("item_id", name="conversation_item_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_item_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="conversation_item_session_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turn.turn_id"], name="conversation_item_turn_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "item_type IN ('prompt','message','reasoning','tool_call')", name="ck_conversation_item_type"
        ),
        sa.CheckConstraint("status IN ('open','complete','failed')", name="ck_conversation_item_status"),
        sa.CheckConstraint("(status = 'open') = (closed_seq IS NULL)", name="ck_conversation_item_open"),
        sa.CheckConstraint(
            "closed_seq IS NULL OR closed_seq >= opened_seq", name="ck_conversation_item_close_after_open"
        ),
        sa.CheckConstraint(
            "(item_type = 'tool_call') = (call_id IS NOT NULL) "
            "AND (item_type = 'tool_call') = (tool_name IS NOT NULL) "
            "AND (item_type = 'tool_call' OR arguments IS NULL) "
            "AND (item_type = 'tool_call' OR outcome IS NULL) "
            "AND (item_type = 'tool_call' OR structured IS NULL)",
            name="ck_conversation_item_tool_call_fields",
        ),
        sa.CheckConstraint(
            "(item_type = 'reasoning' OR disclosure IS NULL) AND (item_type = 'prompt' OR origin IS NULL)",
            name="ck_conversation_item_typed_fields",
        ),
        sa.CheckConstraint(
            "status <> 'complete' OR ((item_type <> 'tool_call' OR outcome IS NOT NULL) "
            "AND (item_type <> 'reasoning' OR disclosure IS NOT NULL))",
            name="ck_conversation_item_complete_terminal_fields",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded','failed','unknown')", name="ck_conversation_item_outcome"
        ),
        sa.CheckConstraint(
            "disclosure IS NULL OR disclosure IN ('summary','withheld')", name="ck_conversation_item_disclosure"
        ),
    )
    op.create_index(
        "uq_conversation_item_call",
        "conversation_item",
        ["conversation_id", "call_id"],
        unique=True,
        postgresql_where=sa.text("call_id IS NOT NULL"),
    )
    op.create_index(
        "uq_conversation_item_runner",
        "conversation_item",
        ["session_id", "runner_item_id"],
        unique=True,
        postgresql_where=sa.text("runner_item_id IS NOT NULL"),
    )
    op.create_index("idx_conversation_item_conversation", "conversation_item", ["conversation_id", "opened_seq"])
    op.create_index("idx_conversation_item_turn", "conversation_item", ["turn_id", "opened_seq"])
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

    op.create_table(
        "conversation_event",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("source_first_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("source_last_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id", "event_seq", name="conversation_event_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_event_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="conversation_event_session_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turn.turn_id"], name="conversation_event_turn_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["conversation_item.item_id"], name="conversation_event_item_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint("event_seq > 0", name="ck_conversation_event_seq_positive"),
        sa.CheckConstraint("provenance IN ('frame_range','authored')", name="ck_conversation_event_provenance"),
        sa.CheckConstraint(
            "(provenance = 'frame_range') = (source_first_frame_seq IS NOT NULL) "
            "AND (source_first_frame_seq IS NULL) = (source_last_frame_seq IS NULL) "
            "AND (source_first_frame_seq IS NULL OR source_first_frame_seq <= source_last_frame_seq) "
            "AND (provenance <> 'frame_range' OR turn_id IS NOT NULL) "
            "AND (provenance <> 'frame_range' OR session_id IS NOT NULL) "
            "AND (provenance <> 'frame_range' OR item_id IS NOT NULL)",
            name="ck_conversation_event_provenance_frames",
        ),
        sa.CheckConstraint(
            "(item_id IS NOT NULL) = (kind IN ('item_opened','item_segment','item_completed'))",
            name="ck_conversation_event_item_kinds",
        ),
    )
    op.create_index("idx_conversation_event_session", "conversation_event", ["session_id", "event_seq"])
    op.create_index("idx_conversation_event_item", "conversation_event", ["item_id", "event_seq"])

    op.create_table(
        "conversation_prompt",
        sa.Column("prompt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("prompt_id", name="conversation_prompt_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="conversation_prompt_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["conversation_item.item_id"], name="conversation_prompt_item_id_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turn.turn_id"], name="conversation_prompt_turn_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by_session_id"],
            ["sessions.session_id"],
            name="conversation_prompt_claimed_by_session_id_fkey",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("item_id", name="uq_conversation_prompt_item"),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claimed_by_session_id IS NULL)", name="ck_conversation_prompt_claim"
        ),
        sa.CheckConstraint(
            "claimed_at IS NULL OR claimed_at >= queued_at", name="ck_conversation_prompt_claim_after_queue"
        ),
    )
    op.create_index(
        "uq_conversation_prompt_unclaimed",
        "conversation_prompt",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("claimed_at IS NULL"),
    )
    op.create_index("idx_conversation_prompt_conversation", "conversation_prompt", ["conversation_id", "queued_at"])

    op.create_table(
        "submitted_prompt",
        sa.Column("prompt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("origin", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("prompt_id", name="submitted_prompt_pkey"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            name="submitted_prompt_conversation_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["admitted_item_id"],
            ["conversation_item.item_id"],
            name="submitted_prompt_admitted_item_id_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("admitted_item_id", name="uq_submitted_prompt_admitted_item"),
        sa.CheckConstraint("btrim(text) <> ''", name="ck_submitted_prompt_text_nonempty"),
        sa.CheckConstraint("admitted_at IS NULL OR withdrawn_at IS NULL", name="ck_submitted_prompt_single_outcome"),
        sa.CheckConstraint(
            "(admitted_at IS NULL) = (admitted_item_id IS NULL)", name="ck_submitted_prompt_admission_pair"
        ),
        sa.CheckConstraint(
            "admitted_at IS NULL OR admitted_at >= submitted_at", name="ck_submitted_prompt_admit_after_submit"
        ),
        sa.CheckConstraint(
            "withdrawn_at IS NULL OR withdrawn_at >= submitted_at", name="ck_submitted_prompt_withdraw_after_submit"
        ),
    )
    op.create_index("idx_submitted_prompt_conversation", "submitted_prompt", ["conversation_id", "submitted_at"])

    op.create_table(
        "session_frames",
        sa.Column("frame_seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSON(), nullable=False),
        sa.Column("runner_seq", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("frame_seq", name="session_frames_pkey"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.session_id"], name="session_frames_session_id_fkey", ondelete="CASCADE"
        ),
        sa.CheckConstraint("direction IN ('to_agent','from_agent')", name="ck_session_frames_direction"),
        sa.CheckConstraint("kind IN ('harness_frame','setup_output')", name="ck_session_frames_kind"),
    )
    op.create_index("idx_session_frames_session", "session_frames", ["session_id", "frame_seq"])
    op.create_index("idx_session_frames_kind", "session_frames", ["session_id", "kind", "frame_seq"])
    op.create_index(
        "uq_session_frames_runner_seq",
        "session_frames",
        ["session_id", "runner_seq"],
        unique=True,
        postgresql_where=sa.text("runner_seq IS NOT NULL"),
    )

    op.create_table(
        "matrix_access_token",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="matrix_access_token_pkey"),
    )

    op.create_table(
        "matrix_sync_watermark",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("next_batch", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="matrix_sync_watermark_pkey"),
    )

    op.create_table(
        "matrix_revision",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("revision_id", name="matrix_revision_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["channel_attachment.attachment_id"],
            name="matrix_revision_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_matrix_revision_subject_nonempty"),
        sa.CheckConstraint("btrim(event_id) <> ''", name="ck_matrix_revision_event_nonempty"),
        sa.CheckConstraint("retired_at IS NULL OR retired_at >= sent_at", name="ck_matrix_revision_retire_after_sent"),
    )
    op.create_index(
        "uq_matrix_revision_live_subject",
        "matrix_revision",
        ["attachment_id", "subject"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )

    op.create_table(
        "matrix_room_copy",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("replaces_event_id", sa.Text(), nullable=True),
        sa.Column("origin_server_ts", sa.BigInteger(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="matrix_room_copy_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["channel_attachment.attachment_id"],
            name="matrix_room_copy_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("btrim(event_id) <> ''", name="ck_matrix_room_copy_event_nonempty"),
        sa.CheckConstraint("source_event_seq > 0", name="ck_matrix_room_copy_source_positive"),
    )
    op.create_index("idx_matrix_room_copy_source", "matrix_room_copy", ["attachment_id", "source_event_seq"])

    op.create_table(
        "matrix_outbox",
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("outbox_id", name="matrix_outbox_pkey"),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["channel_attachment.attachment_id"],
            name="matrix_outbox_attachment_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_matrix_outbox_subject_nonempty"),
    )
    op.create_index(
        "idx_matrix_outbox_unsent",
        "matrix_outbox",
        ["attachment_id", "created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_index("uq_matrix_outbox_subject", "matrix_outbox", ["attachment_id", "subject"], unique=True)

    op.create_table(
        "matrix_ingress_event",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("prompt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="matrix_ingress_event_pkey"),
        sa.ForeignKeyConstraint(
            ["prompt_id"],
            ["submitted_prompt.prompt_id"],
            name="matrix_ingress_event_prompt_id_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_matrix_ingress_event_prompt", "matrix_ingress_event", ["prompt_id"])

    op.create_foreign_key(
        "fk_mcp_tool_call_principals_session_binding",
        "mcp_tool_call_principals",
        "sessions",
        ["session_id", "binding_id"],
        ["session_id", "agent_binding_id"],
        ondelete="RESTRICT",
    )

    for table in _GRANT_TABLES:
        op.add_column(table, sa.Column("principal_session_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"{table}_principal_session_id_fkey",
            table,
            "sessions",
            ["principal_session_id"],
            ["session_id"],
            ondelete="RESTRICT",
        )
        op.drop_constraint(f"ck_{table}_principal_shape", table, type_="check")
        op.create_check_constraint(f"ck_{table}_principal_shape", table, _PRINCIPAL_SHAPE_WITH_SESSION)

    op.execute(f"DROP TRIGGER {_KUBERNETES_TRIGGER} ON public.kubernetes_grants")
    op.execute(_SOURCE_FUNCTION_WITH_SESSION)
    op.execute(
        f"""
CREATE TRIGGER {_KUBERNETES_TRIGGER}
BEFORE INSERT OR UPDATE OF {_KUBERNETES_TRIGGER_COLUMNS_WITH_SESSION}
ON public.kubernetes_grants
FOR EACH ROW EXECUTE FUNCTION {_SOURCE_FUNCTION}
"""
    )
