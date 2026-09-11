"""Drop the node-daemon broker tables: node_daemon_executions, node_daemon_presence.

The `hostexec` MCP tool that submitted work through this broker is gone (see
`haku-console: remove the hostexec MCP tool`), and with it the `hostexecd` daemon and
every caller of `/api/node-daemons/v1/*`. The broker and its Postgres tables have no
remaining caller.

Revision ID: 0131
Revises: 0130
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0131"
down_revision: str | None = "0130"
branch_labels: str | None = None
depends_on: str | None = None

_NODE_DAEMON_EXECUTION_STATUS_VALUES = ("pending", "claimed", "succeeded", "failed")


def upgrade() -> None:
    op.drop_table("node_daemon_executions")
    op.drop_table("node_daemon_presence")
    op.execute("DROP TYPE node_daemon_execution_status")


def downgrade() -> None:
    node_daemon_execution_status = postgresql.ENUM(
        *_NODE_DAEMON_EXECUTION_STATUS_VALUES, name="node_daemon_execution_status"
    )
    node_daemon_execution_status.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "node_daemon_executions",
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("daemon_id", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("status", node_daemon_execution_status, nullable=False),
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
        sa.PrimaryKeyConstraint("execution_id", name="node_daemon_executions_pkey"),
    )
    op.create_index(
        "idx_node_daemon_executions_dispatch", "node_daemon_executions", ["daemon_id", "status", "created_at"]
    )

    op.create_table(
        "node_daemon_presence",
        sa.Column("daemon_id", sa.Text(), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("backends_json", postgresql.JSONB(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("daemon_id", name="node_daemon_presence_pkey"),
    )
