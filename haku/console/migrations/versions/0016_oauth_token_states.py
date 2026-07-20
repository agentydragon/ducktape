"""Extract shared Operator OAuth token state.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNERS = (
    ("mcp_operator_oauth_associations", "fk_mcp_operator_oauth_associations_token_state"),
    ("provider_connections", "fk_provider_connections_token_state"),
    ("operator_authentik_tokens", "fk_operator_authentik_tokens_token_state"),
)
_TOKEN_COLUMNS = (
    "token_revision",
    "updated_at",
    "access_token",
    "refresh_token",
    "token_type",
    "scope",
    "token_expires_at",
)


def _insert_token_states(owner: str) -> None:
    op.execute(
        sa.text(
            f"""
            INSERT INTO oauth_token_states (
                token_state_id, operator_id, token_revision, created_at, updated_at,
                access_token, refresh_token, token_type, scope, token_expires_at
            )
            SELECT
                token_state_id, operator_id, token_revision, created_at, updated_at,
                access_token, refresh_token, token_type, scope, token_expires_at
            FROM {owner}
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "oauth_token_states",
        sa.Column("token_state_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("refresh_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_state_id", "operator_id", name="uq_oauth_token_states_id_operator"),
        sa.CheckConstraint(
            "(refresh_claim_id IS NULL) = (refresh_claim_expires_at IS NULL)",
            name="ck_oauth_token_states_refresh_claim_shape",
        ),
    )
    op.create_index(
        "idx_oauth_token_states_refresh_candidates",
        "oauth_token_states",
        ["token_expires_at"],
        postgresql_where=sa.text("refresh_token IS NOT NULL AND token_expires_at IS NOT NULL"),
    )

    for owner, _constraint in _OWNERS:
        op.add_column(owner, sa.Column("token_state_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.execute(sa.text(f"UPDATE {owner} SET token_state_id = gen_random_uuid()"))
        _insert_token_states(owner)
        op.alter_column(owner, "token_state_id", nullable=False)

    op.drop_constraint(
        "fk_mcp_operator_oauth_associations_operator", "mcp_operator_oauth_associations", type_="foreignkey"
    )
    op.drop_constraint("fk_provider_connections_operator", "provider_connections", type_="foreignkey")
    op.drop_constraint("fk_operator_authentik_tokens_operator", "operator_authentik_tokens", type_="foreignkey")
    for owner, constraint in _OWNERS:
        op.create_unique_constraint(f"uq_{owner}_token_state_id", owner, ["token_state_id"])
        op.create_foreign_key(
            constraint,
            owner,
            "oauth_token_states",
            ["token_state_id", "operator_id"],
            ["token_state_id", "operator_id"],
            ondelete="CASCADE",
        )
        for column in _TOKEN_COLUMNS:
            op.drop_column(owner, column)

    op.execute(
        """
        CREATE FUNCTION validate_oauth_token_state_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
                (SELECT count(*) FROM mcp_operator_oauth_associations WHERE token_state_id = COALESCE(NEW.token_state_id, OLD.token_state_id))
              + (SELECT count(*) FROM provider_connections WHERE token_state_id = COALESCE(NEW.token_state_id, OLD.token_state_id))
              + (SELECT count(*) FROM operator_authentik_tokens WHERE token_state_id = COALESCE(NEW.token_state_id, OLD.token_state_id))
              INTO owners;
            IF owners <> 1 THEN
                RAISE EXCEPTION 'OAuth token state must have exactly one owner';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    for table in ("oauth_token_states", *(owner for owner, _constraint in _OWNERS)):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER trg_{table}_token_state_owner
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_oauth_token_state_owner()
            """
        )


def downgrade() -> None:
    for table in ("oauth_token_states", *(owner for owner, _constraint in _OWNERS)):
        op.execute(f"DROP TRIGGER trg_{table}_token_state_owner ON {table}")
    op.execute("DROP FUNCTION validate_oauth_token_state_owner()")

    for owner, constraint in _OWNERS:
        op.add_column(owner, sa.Column("token_revision", sa.BigInteger(), nullable=True))
        op.add_column(owner, sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(owner, sa.Column("access_token", sa.Text(), nullable=True))
        op.add_column(owner, sa.Column("refresh_token", sa.Text(), nullable=True))
        op.add_column(owner, sa.Column("token_type", sa.Text(), nullable=True))
        op.add_column(owner, sa.Column("scope", sa.Text(), nullable=True))
        op.add_column(owner, sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(
            sa.text(
                f"""
                UPDATE {owner} AS owner SET
                    token_revision = state.token_revision,
                    updated_at = state.updated_at,
                    access_token = state.access_token,
                    refresh_token = state.refresh_token,
                    token_type = state.token_type,
                    scope = state.scope,
                    token_expires_at = state.token_expires_at
                FROM oauth_token_states AS state
                WHERE state.token_state_id = owner.token_state_id
                """
            )
        )
        for column in ("token_revision", "updated_at", "access_token", "token_type"):
            op.alter_column(owner, column, nullable=False)
        op.drop_constraint(constraint, owner, type_="foreignkey")
        op.drop_constraint(f"uq_{owner}_token_state_id", owner, type_="unique")
        op.drop_column(owner, "token_state_id")

    op.create_foreign_key(
        "fk_mcp_operator_oauth_associations_operator",
        "mcp_operator_oauth_associations",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_provider_connections_operator",
        "provider_connections",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_operator_authentik_tokens_operator",
        "operator_authentik_tokens",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )
    op.drop_table("oauth_token_states")
