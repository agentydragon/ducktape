"""Add durable outbound node-daemon presence and execution leases.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    execution_status = postgresql.ENUM(
        "pending", "claimed", "succeeded", "failed", name="node_daemon_execution_status", create_type=False
    )
    postgresql.ENUM("pending", "claimed", "succeeded", "failed", name="node_daemon_execution_status").create(
        op.get_bind(), checkfirst=True
    )
    op.create_table(
        "node_daemon_presence",
        sa.Column("daemon_id", sa.Text(), primary_key=True),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("backends_json", postgresql.JSONB(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "node_daemon_executions",
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("daemon_id", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("status", execution_status, nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_token_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_node_daemon_executions_dispatch", "node_daemon_executions", ["daemon_id", "status", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("node_daemon_executions")
    op.drop_table("node_daemon_presence")
    postgresql.ENUM(name="node_daemon_execution_status").drop(op.get_bind(), checkfirst=True)
