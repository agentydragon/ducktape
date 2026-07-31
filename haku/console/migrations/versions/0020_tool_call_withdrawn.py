"""Let a submitting Agent withdraw its own still-pending tool call.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOOL_CALL_STATUS_WITHOUT_WITHDRAWN = ("pending_approval", "running", "ok", "error", "denied")


def upgrade() -> None:
    # PostgreSQL 12+ allows ALTER TYPE ... ADD VALUE inside a transaction block (env.py runs the
    # whole upgrade in one) as long as the new label is not *used* before commit — this migration
    # only adds a nullable column. Appended with no BEFORE/AFTER so pg_enum's enumsortorder keeps
    # matching ToolCallStatus's declaration order, which
    # test_mcp_approval.test_fresh_baseline_enum_values_match_domain_enums asserts.
    op.execute("ALTER TYPE tool_call_status ADD VALUE 'withdrawn'")
    op.add_column("mcp_tool_calls", sa.Column("withdrawal_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    # Lossy by nature: a withdrawal is a terminal audit fact with no pre-0020 representation, so
    # the closest surviving state is `denied` with the reason carried into denial_reason.
    op.execute(
        "UPDATE mcp_tool_calls "
        "SET status = 'denied', denial_reason = 'withdrawn by agent: ' || coalesce(withdrawal_reason, '') "
        "WHERE status = 'withdrawn'"
    )
    op.drop_column("mcp_tool_calls", "withdrawal_reason")
    # PostgreSQL cannot drop an enum label in place; the type has to be rebuilt around the column.
    op.execute("ALTER TYPE tool_call_status RENAME TO tool_call_status_old")
    sa.Enum(*_TOOL_CALL_STATUS_WITHOUT_WITHDRAWN, name="tool_call_status").create(op.get_bind())
    op.execute(
        "ALTER TABLE mcp_tool_calls ALTER COLUMN status TYPE tool_call_status USING status::text::tool_call_status"
    )
    op.execute("DROP TYPE tool_call_status_old")
