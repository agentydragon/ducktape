"""Drop provider_connections, provider_connection_flows, oauth_connection_results, and
oauth_token_states.

The Operator-linked Google account subsystem (`gmail`/`google_calendar` in-process MCP servers,
their auto-approval policy, and this OAuth account-linking flow) has been decommissioned: the
deployed catalog never registered those servers, and Agentplane's own `google-mcp` reaches
Gmail/Calendar independently through an Airlock-minted credential. `provider_connections` was the
last remaining owner of `oauth_token_states` rows (0134 retired `operator_authentik_tokens`, 0132
retired `mcp_operator_oauth_associations`), so the shared token-state table and its ownership
trigger function go with it; the flow and result tables have no remaining writer either.

Revision ID: 0135
Revises: 0134
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0135"
down_revision: str | None = "0134"
branch_labels: str | None = None
depends_on: str | None = None


def _token_state_owner_function(owner_tables: tuple[str, ...]) -> str:
    owner_counts = "\n              + ".join(
        f"(SELECT count(*) FROM {table} WHERE token_state_id = COALESCE(NEW.token_state_id, OLD.token_state_id))"
        for table in owner_tables
    )
    return f"""
CREATE OR REPLACE FUNCTION public.validate_oauth_token_state_owner()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        DECLARE
            owners integer;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.token_state_id IS DISTINCT FROM NEW.token_state_id THEN
                RAISE EXCEPTION 'OAuth token-state ownership is immutable';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM oauth_token_states WHERE token_state_id = COALESCE(NEW.token_state_id, OLD.token_state_id)
            ) THEN
                RETURN NULL;
            END IF;
            SELECT
                {owner_counts}
              INTO owners;
            IF owners <> 1 THEN
                RAISE EXCEPTION 'OAuth token state must have exactly one owner';
            END IF;
            RETURN NULL;
        END;
        $function$
"""


def upgrade() -> None:
    # Table drops remove their rows directly (no per-row trigger firing), so no owner rebalancing
    # is needed before dropping provider_connections, unlike 0132/0134's row-level DELETEs.
    op.drop_index("idx_provider_connections_operator", table_name="provider_connections")
    op.drop_table("provider_connections")
    op.drop_index("idx_provider_connection_flows_expires_at", table_name="provider_connection_flows")
    op.drop_index("idx_provider_connection_flows_operator", table_name="provider_connection_flows")
    op.drop_table("provider_connection_flows")
    op.drop_index("idx_oauth_connection_results_expires_at", table_name="oauth_connection_results")
    op.drop_index("idx_oauth_connection_results_operator", table_name="oauth_connection_results")
    op.drop_table("oauth_connection_results")
    # provider_connections was the sole remaining owner; with it gone, oauth_token_states and its
    # ownership trigger function have no more callers.
    op.drop_index("idx_oauth_token_states_refresh_candidates", table_name="oauth_token_states")
    op.drop_table("oauth_token_states")
    op.execute("DROP FUNCTION public.validate_oauth_token_state_owner()")


def downgrade() -> None:
    op.create_table(
        "oauth_token_states",
        sa.Column("token_state_id", UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_claim_id", UUID(as_uuid=True), nullable=True),
        sa.Column("refresh_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_initial_kind", sa.Text(), nullable=True),
        sa.Column("refresh_failure_initial_message", sa.Text(), nullable=True),
        sa.Column("refresh_failure_latest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_latest_kind", sa.Text(), nullable=True),
        sa.Column("refresh_failure_latest_message", sa.Text(), nullable=True),
        sa.Column("refresh_failure_count", sa.BigInteger(), server_default=sa.text("'0'::bigint"), nullable=False),
        sa.Column("refresh_failure_action", sa.Text(), nullable=True),
        sa.Column("refresh_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_state_id", name="oauth_token_states_pkey"),
        sa.CheckConstraint(
            "((refresh_claim_id IS NULL) = (refresh_claim_expires_at IS NULL))",
            name="ck_oauth_token_states_refresh_claim_shape",
        ),
        sa.CheckConstraint(
            "(((refresh_failure_count = 0) AND (refresh_failure_started_at IS NULL) AND "
            "(refresh_failure_initial_kind IS NULL) AND (refresh_failure_initial_message IS NULL) AND "
            "(refresh_failure_latest_at IS NULL) AND (refresh_failure_latest_kind IS NULL) AND "
            "(refresh_failure_latest_message IS NULL) AND (refresh_failure_action IS NULL) AND "
            "(refresh_retry_at IS NULL)) OR ((refresh_failure_count > 0) AND "
            "(refresh_failure_started_at IS NOT NULL) AND (refresh_failure_initial_kind IS NOT NULL) AND "
            "(refresh_failure_initial_message IS NOT NULL) AND (refresh_failure_latest_at IS NOT NULL) AND "
            "(refresh_failure_latest_kind IS NOT NULL) AND (refresh_failure_latest_message IS NOT NULL) AND "
            "(((refresh_failure_action = 'retrying'::text) AND (refresh_retry_at IS NOT NULL)) OR "
            "((refresh_failure_action = ANY (ARRAY['reconnect'::text, 'operator_action'::text])) AND "
            "(refresh_retry_at IS NULL)))))",
            name="ck_oauth_token_states_refresh_failure_shape",
        ),
        sa.UniqueConstraint("token_state_id", "operator_id", name="uq_oauth_token_states_id_operator"),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="oauth_token_states_operator_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_oauth_token_states_refresh_candidates",
        "oauth_token_states",
        ["token_expires_at"],
        postgresql_where=sa.text("((refresh_token IS NOT NULL) AND (token_expires_at IS NOT NULL))"),
    )
    op.execute(_token_state_owner_function(("provider_connections",)))
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_oauth_token_states_token_state_owner AFTER INSERT OR DELETE OR UPDATE ON public.oauth_token_states DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_oauth_token_state_owner()
        """
    )
    op.create_table(
        "oauth_connection_results",
        sa.Column("result_id", UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("result_id", name="oauth_connection_results_pkey"),
        sa.CheckConstraint("btrim(message) <> ''", name="ck_oauth_connection_results_message_nonempty"),
        sa.CheckConstraint("status IN ('success', 'error')", name="ck_oauth_connection_results_status"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_oauth_connection_results_title_nonempty"),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operators.operator_id"],
            name="oauth_connection_results_operator_id_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_oauth_connection_results_operator", "oauth_connection_results", ["operator_id"])
    op.create_index("idx_oauth_connection_results_expires_at", "oauth_connection_results", ["expires_at"])
    op.create_table(
        "provider_connection_flows",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("connection_name", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("state", name="provider_connection_flows_pkey"),
        sa.CheckConstraint(
            "btrim(connection_name) <> ''", name="ck_provider_connection_flows_connection_name_nonempty"
        ),
        sa.CheckConstraint("btrim(provider_name) <> ''", name="ck_provider_connection_flows_provider_name_nonempty"),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="fk_provider_connection_flows_operator", ondelete="CASCADE"
        ),
    )
    op.create_index("idx_provider_connection_flows_operator", "provider_connection_flows", ["operator_id"])
    op.create_index("idx_provider_connection_flows_expires_at", "provider_connection_flows", ["expires_at"])
    op.create_table(
        "provider_connections",
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connection_name", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("token_state_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("operator_id", "connection_name", name="provider_connections_pkey"),
        sa.CheckConstraint("btrim(connection_name) <> ''", name="ck_provider_connections_connection_name_nonempty"),
        sa.CheckConstraint("btrim(provider_name) <> ''", name="ck_provider_connections_provider_name_nonempty"),
        sa.UniqueConstraint("connection_id", name="uq_provider_connections_connection_id"),
        sa.UniqueConstraint("token_state_id", name="uq_provider_connections_token_state_id"),
        sa.ForeignKeyConstraint(
            ["token_state_id", "operator_id"],
            ["oauth_token_states.token_state_id", "oauth_token_states.operator_id"],
            name="fk_provider_connections_token_state",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_provider_connections_operator", "provider_connections", ["operator_id"])
    op.execute(_token_state_owner_function(("provider_connections",)))
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_provider_connections_token_state_owner AFTER INSERT OR DELETE OR UPDATE ON public.provider_connections DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_oauth_token_state_owner()
        """
    )
