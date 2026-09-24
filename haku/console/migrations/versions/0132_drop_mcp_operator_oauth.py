"""Drop the remote-MCP operator OAuth tables: mcp_operator_oauth_associations, mcp_operator_oauth_flows.

The console reaches only in-process MCP servers now, so no operator links an account at a remote
MCP server's authorization server. Each association owned one `oauth_token_states` row holding a
refresh token; those rows are deleted with it rather than left without an owner.

Revision ID: 0132
Revises: 0131
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0132"
down_revision: str | None = "0131"
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


_REMAINING_OWNERS = ("provider_connections", "operator_authentik_tokens")


def upgrade() -> None:
    # Deleting the token state cascades to its association.
    op.execute(
        """
        DELETE FROM oauth_token_states
        WHERE token_state_id IN (SELECT token_state_id FROM mcp_operator_oauth_associations)
        """
    )
    # The ownership triggers are deferred, and a table with pending trigger events cannot be dropped.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(_token_state_owner_function(_REMAINING_OWNERS))
    op.drop_table("mcp_operator_oauth_associations")
    op.drop_table("mcp_operator_oauth_flows")


def downgrade() -> None:
    op.create_table(
        "mcp_operator_oauth_flows",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("client_secret_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("state", name="mcp_operator_oauth_flows_pkey"),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="fk_mcp_operator_oauth_flows_operator", ondelete="CASCADE"
        ),
    )
    op.create_index("idx_mcp_operator_oauth_flows_expires_at", "mcp_operator_oauth_flows", ["expires_at"])
    op.create_index(
        "idx_mcp_operator_oauth_flows_server_operator", "mcp_operator_oauth_flows", ["server_id", "operator_id"]
    )
    op.create_table(
        "mcp_operator_oauth_associations",
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("client_secret_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("association_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_state_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("server_id", "operator_id", name="mcp_operator_oauth_associations_pkey"),
        sa.UniqueConstraint("association_id", name="uq_mcp_operator_oauth_associations_association_id"),
        sa.UniqueConstraint("token_state_id", name="uq_mcp_operator_oauth_associations_token_state_id"),
        sa.ForeignKeyConstraint(
            ["token_state_id", "operator_id"],
            ["oauth_token_states.token_state_id", "oauth_token_states.operator_id"],
            name="fk_mcp_operator_oauth_associations_token_state",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_mcp_operator_oauth_associations_operator", "mcp_operator_oauth_associations", ["operator_id"])
    op.execute(_token_state_owner_function(("mcp_operator_oauth_associations", *_REMAINING_OWNERS)))
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_mcp_operator_oauth_associations_token_state_owner AFTER INSERT OR DELETE OR UPDATE ON public.mcp_operator_oauth_associations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_oauth_token_state_owner()
        """
    )
