"""Stable history-entry cursor for bounded operator history pages."""

import sqlalchemy as sa
from alembic import op

revision = "0019_action_history_page"
down_revision = "0018_mcp_refresh_failure_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_request", sa.Column("history_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("""UPDATE action_request AS r SET history_at = (
        SELECT MIN(e.at) FROM action_event AS e
        WHERE e.request_id = r.id AND e.state <> 'decision_pending'
    ) WHERE r.state <> 'decision_pending'""")
    op.create_index(
        "action_request_history_page",
        "action_request",
        [sa.text("history_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("history_at IS NOT NULL"),
    )
    op.create_index(
        "action_request_pending_page",
        "action_request",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("state = 'decision_pending'"),
    )


def downgrade() -> None:
    op.drop_index("action_request_pending_page", table_name="action_request")
    op.drop_index("action_request_history_page", table_name="action_request")
    op.drop_column("action_request", "history_at")
