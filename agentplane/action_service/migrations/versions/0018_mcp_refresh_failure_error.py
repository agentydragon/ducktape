"""A token refresh failure keeps what went wrong, beside the action it calls for.

Failure bookkeeping from before this revision has no error to carry, so the upgrade clears it: the
next refresh sweep retries a due token, and a refusal is recorded again, this time with its error.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_mcp_refresh_failure_error"
down_revision = "0017_drop_mcp_linkage_provider"
branch_labels = None
depends_on = None

_WHOLE = "(refresh_failure_action IS NULL) = (refresh_failure_error IS NULL)"


def upgrade() -> None:
    op.add_column("mcp_oauth_token_state", sa.Column("refresh_failure_error", sa.Text(), nullable=True))
    op.execute(
        "UPDATE mcp_oauth_token_state SET refresh_failure_count = 0, refresh_failure_started_at = NULL, "
        "refresh_failure_latest_at = NULL, refresh_failure_action = NULL, refresh_retry_at = NULL"
    )
    op.create_check_constraint("mcp_oauth_token_state_refresh_failure_whole", "mcp_oauth_token_state", _WHOLE)


def downgrade() -> None:
    op.drop_constraint("mcp_oauth_token_state_refresh_failure_whole", "mcp_oauth_token_state", type_="check")
    op.drop_column("mcp_oauth_token_state", "refresh_failure_error")
