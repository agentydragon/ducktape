"""Persist principal-scoped temporary HTTP egress grants and their provenance.

A sibling of the Kubernetes grant domain: same ownership, principal, and provenance columns. The
capability is one exact canonical public origin in three relational columns ``(scheme, host,
port)`` narrowed by a JSONB method set and an optional path regex. Coverage validation is
app-side (`http_grant_models`); Postgres holds only the relational invariants. Status is derived
from the end facts (``released_at``/``revoked_at``, `http_grant_models.derive_status`), never
stored.

Revision ID: 0102
Revises: 0101
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

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
        sa.Column("methods", JSONB(), nullable=False),
        sa.Column("path_regex", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("expires_at > created_at", name="ck_http_grants_expiration_after_creation"),
        sa.CheckConstraint(
            "num_nonnulls(released_at, revoked_at) <= 1 "
            "AND ((num_nonnulls(released_at, revoked_at) = 1) = (end_reason IS NOT NULL)) "
            "AND (end_reason IS NULL OR btrim(end_reason) <> '')",
            name="ck_http_grants_end_shape",
        ),
    )
    op.create_index("idx_http_grants_source_tool_call", "http_grants", ["source_tool_call_id"])
    op.create_index("idx_http_grants_owner_expiry", "http_grants", ["owner_agent_id", "expires_at"])
    op.create_index("idx_http_grants_agent_principal_expiry", "http_grants", ["principal_agent_id", "expires_at"])
    op.create_index("idx_http_grants_session_principal_expiry", "http_grants", ["principal_session_id", "expires_at"])


def downgrade() -> None:
    op.drop_index("idx_http_grants_session_principal_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_agent_principal_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_owner_expiry", table_name="http_grants")
    op.drop_index("idx_http_grants_source_tool_call", table_name="http_grants")
    op.drop_table("http_grants")
