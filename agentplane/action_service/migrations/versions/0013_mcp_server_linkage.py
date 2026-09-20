"""Store shared MCP OAuth linkage, normalized token state, and PKCE flows."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_mcp_server_linkage"
down_revision = "0012_action_push_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_token_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("server_id", sa.Text(), nullable=False, unique=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("refresh_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_count", sa.Integer(), nullable=False),
        sa.Column("refresh_failure_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_latest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_action", sa.Text(), nullable=True),
        sa.Column("refresh_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "mcp_server_linkage",
        sa.Column("server_id", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("server_url", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "token_state_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_oauth_token_state.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("token_endpoint", sa.Text(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("linked_by", sa.Text(), nullable=True),
    )
    op.create_table(
        "mcp_linkage_flow",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("verifier", sa.Text(), nullable=False),
        sa.Column("operator_principal", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.Column("authorization_endpoint", sa.Text(), nullable=False),
        sa.Column("token_endpoint", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mcp_linkage_flow")
    op.drop_table("mcp_server_linkage")
    op.drop_table("mcp_oauth_token_state")
