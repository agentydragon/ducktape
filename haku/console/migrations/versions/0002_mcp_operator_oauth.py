"""Add MCP operator OAuth associations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import BigInteger, Column, DateTime, Text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_operator_oauth_associations",
        Column("server_id", Text, primary_key=True),
        Column("operator_principal", Text, primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("client_id", Text, nullable=False),
        Column("client_secret", Text, nullable=True),
        Column("client_secret_expires_at", BigInteger, nullable=True),
        Column("token_endpoint_auth_method", Text, nullable=True),
        Column("token_endpoint", Text, nullable=False),
        Column("resource", Text, nullable=True),
        Column("access_token", Text, nullable=False),
        Column("refresh_token", Text, nullable=True),
        Column("token_type", Text, nullable=False),
        Column("scope", Text, nullable=True),
        Column("token_expires_at", DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_mcp_operator_oauth_associations_operator", "mcp_operator_oauth_associations", ["operator_principal"]
    )
    op.create_table(
        "mcp_operator_oauth_flows",
        Column("state", Text, primary_key=True),
        Column("server_id", Text, nullable=False),
        Column("operator_principal", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("redirect_uri", Text, nullable=False),
        Column("code_verifier", Text, nullable=False),
        Column("client_id", Text, nullable=False),
        Column("client_secret", Text, nullable=True),
        Column("client_secret_expires_at", BigInteger, nullable=True),
        Column("token_endpoint_auth_method", Text, nullable=True),
        Column("token_endpoint", Text, nullable=False),
        Column("resource", Text, nullable=True),
        Column("scope", Text, nullable=True),
    )
    op.create_index(
        "idx_mcp_operator_oauth_flows_server_operator", "mcp_operator_oauth_flows", ["server_id", "operator_principal"]
    )
    op.create_index("idx_mcp_operator_oauth_flows_expires_at", "mcp_operator_oauth_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_mcp_operator_oauth_flows_expires_at", table_name="mcp_operator_oauth_flows")
    op.drop_index("idx_mcp_operator_oauth_flows_server_operator", table_name="mcp_operator_oauth_flows")
    op.drop_table("mcp_operator_oauth_flows")
    op.drop_index("idx_mcp_operator_oauth_associations_operator", table_name="mcp_operator_oauth_associations")
    op.drop_table("mcp_operator_oauth_associations")
