"""Own every tool call and event by an operator OIDC subject.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The old ledger did not persist an owner. Current OAuth associations and DCR mappings are
    # mutable (disconnect/relink), and 0006 merely renamed legacy username values rather than
    # converting them to OIDC subjects, so no live table can safely infer historical ownership.
    # Delete every legacy row rather than guessing an owner or retaining a second, unscoped copy
    # of operator data. The active ledger only contains rows created under the tenant-aware schema.
    op.execute("DELETE FROM mcp_tool_call_events")
    op.execute("DELETE FROM mcp_tool_calls")

    # Active tables are now empty, so PostgreSQL can add the tenant key as NOT NULL without a fake
    # default. Every post-migration insert must supply its authenticated operator subject.
    op.add_column("mcp_tool_calls", sa.Column("operator_subject", sa.Text(), nullable=False))
    op.add_column("mcp_tool_call_events", sa.Column("operator_subject", sa.Text(), nullable=False))
    op.create_index(
        "idx_mcp_tool_calls_operator_subject_created_at", "mcp_tool_calls", ["operator_subject", "created_at"]
    )
    op.create_index(
        "idx_mcp_tool_call_events_operator_subject_event_id", "mcp_tool_call_events", ["operator_subject", "event_id"]
    )


def downgrade() -> None:
    # Dropping ownership would make multi-operator rows global to the old application. Keep this
    # migration explicitly forward-only rather than offering an unsafe downgrade that leaks data.
    raise RuntimeError("0007 is forward-only: removing tool-call ownership would cross tenant boundaries")
