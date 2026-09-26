"""Drop the http_grants table.

The HTTP egress grant domain (`grants.http`) has no enforcement point: no deployed proxy or
route ever calls its decision method (`GrantService.match_request`/`match_tunnel`), and
`credential_handle` never resolved to an actual credential anywhere in this codebase. Only the
Kubernetes grant domain, with its live `haku-kube-api-proxy` enforcement point, remains.

Revision ID: 0133
Revises: 0132
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0133"
down_revision: str | None = "0132"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("idx_http_grants_access_profile_principal_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_agent_principal_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_owner_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_source_tool_call", table_name="http_grants")
    op.drop_table("http_grants")


def downgrade() -> None:
    op.create_table(
        "http_grants",
        sa.Column("grant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("principal_kind", sa.Text(), nullable=False),
        sa.Column("principal_agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("principal_access_profile_id", sa.Text(), nullable=True),
        sa.Column("source_tool_call_id", sa.Text(), nullable=False),
        sa.Column("scheme", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("methods", JSONB(), nullable=False),
        sa.Column("path_regex", sa.Text(), nullable=True),
        sa.Column("credential_handle", sa.Text(), nullable=True),
        sa.Column("allow_prohibited_address", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            ["source_tool_call_id"],
            ["mcp_tool_calls.tool_call_id"],
            name="http_grants_source_tool_call_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("btrim(source_tool_call_id) <> ''", name="ck_http_grants_source_tool_call_nonempty"),
        sa.CheckConstraint(
            "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
            "AND principal_access_profile_id IS NULL) OR "
            "(principal_kind = 'access_profile' AND principal_agent_id IS NULL "
            "AND principal_access_profile_id IS NOT NULL)",
            name="ck_http_grants_principal_shape",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_http_grants_expiration_after_creation"
        ),
        sa.CheckConstraint(
            "(ended_at IS NOT NULL OR end_reason IS NULL) AND (end_reason IS NULL OR btrim(end_reason) <> '')",
            name="ck_http_grants_end_shape",
        ),
        sa.CheckConstraint(
            "credential_handle IS NULL OR btrim(credential_handle) <> ''",
            name="ck_http_grants_credential_handle_nonempty",
        ),
    )
    op.create_index("idx_http_grants_source_tool_call", "http_grants", ["source_tool_call_id"])
    op.create_index("idx_http_grants_owner_expiry", "http_grants", ["owner_agent_id", "expires_at"])
    op.create_index("idx_http_grants_agent_principal_expiry", "http_grants", ["principal_agent_id", "expires_at"])
    op.create_index(
        "idx_http_grants_access_profile_principal_expiry", "http_grants", ["principal_access_profile_id", "expires_at"]
    )
