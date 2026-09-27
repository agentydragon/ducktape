"""Drop the operator_authentik_tokens table.

The `hostexec` MCP tool (Tier 1) that exchanged this token for a per-host token was already
removed, and nothing in the app writes or reads this table any more: the operator-login-identity
credential path (`OperatorLoginIdentityCredential`), the `offline_access` OAuth scope that would
capture the token, and the background refresh sweep for it are all dead code removed alongside
this migration. `provider_connections` remains the sole owner of `oauth_token_states` rows.

Revision ID: 0134
Revises: 0133
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0134"
down_revision: str | None = "0133"
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


_REMAINING_OWNERS = ("provider_connections",)


def upgrade() -> None:
    # Deleting the token state cascades to its operator_authentik_tokens row.
    op.execute(
        """
        DELETE FROM oauth_token_states
        WHERE token_state_id IN (SELECT token_state_id FROM operator_authentik_tokens)
        """
    )
    # The ownership triggers are deferred, and a table with pending trigger events cannot be dropped.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(_token_state_owner_function(_REMAINING_OWNERS))
    op.drop_table("operator_authentik_tokens")


def downgrade() -> None:
    op.create_table(
        "operator_authentik_tokens",
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_state_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("operator_id", name="operator_authentik_tokens_pkey"),
        sa.UniqueConstraint("token_state_id", name="uq_operator_authentik_tokens_token_state_id"),
        sa.ForeignKeyConstraint(
            ["token_state_id", "operator_id"],
            ["oauth_token_states.token_state_id", "oauth_token_states.operator_id"],
            name="fk_operator_authentik_tokens_token_state",
            ondelete="CASCADE",
        ),
    )
    op.execute(_token_state_owner_function(("operator_authentik_tokens", *_REMAINING_OWNERS)))
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_operator_authentik_tokens_token_state_owner AFTER INSERT OR DELETE OR UPDATE ON public.operator_authentik_tokens DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_oauth_token_state_owner()
        """
    )
