"""Persist OAuth refresh failure episodes and retire Tana operator OAuth.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FAILURE_COLUMNS = (
    "refresh_failure_started_at",
    "refresh_failure_initial_kind",
    "refresh_failure_initial_message",
    "refresh_failure_latest_at",
    "refresh_failure_latest_kind",
    "refresh_failure_latest_message",
    "refresh_failure_count",
    "refresh_failure_action",
    "refresh_retry_at",
)


def upgrade() -> None:
    op.add_column("oauth_token_states", sa.Column("refresh_failure_started_at", sa.DateTime(timezone=True)))
    op.add_column("oauth_token_states", sa.Column("refresh_failure_initial_kind", sa.Text()))
    op.add_column("oauth_token_states", sa.Column("refresh_failure_initial_message", sa.Text()))
    op.add_column("oauth_token_states", sa.Column("refresh_failure_latest_at", sa.DateTime(timezone=True)))
    op.add_column("oauth_token_states", sa.Column("refresh_failure_latest_kind", sa.Text()))
    op.add_column("oauth_token_states", sa.Column("refresh_failure_latest_message", sa.Text()))
    op.add_column(
        "oauth_token_states", sa.Column("refresh_failure_count", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column("oauth_token_states", sa.Column("refresh_failure_action", sa.Text()))
    op.add_column("oauth_token_states", sa.Column("refresh_retry_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_oauth_token_states_refresh_failure_shape",
        "oauth_token_states",
        """
        (refresh_failure_count = 0
            AND refresh_failure_started_at IS NULL
            AND refresh_failure_initial_kind IS NULL
            AND refresh_failure_initial_message IS NULL
            AND refresh_failure_latest_at IS NULL
            AND refresh_failure_latest_kind IS NULL
            AND refresh_failure_latest_message IS NULL
            AND refresh_failure_action IS NULL
            AND refresh_retry_at IS NULL)
        OR
        (refresh_failure_count > 0
            AND refresh_failure_started_at IS NOT NULL
            AND refresh_failure_initial_kind IS NOT NULL
            AND refresh_failure_initial_message IS NOT NULL
            AND refresh_failure_latest_at IS NOT NULL
            AND refresh_failure_latest_kind IS NOT NULL
            AND refresh_failure_latest_message IS NOT NULL
            AND ((refresh_failure_action = 'retrying' AND refresh_retry_at IS NOT NULL)
                OR (refresh_failure_action IN ('reconnect', 'operator_action') AND refresh_retry_at IS NULL)))
        """,
    )

    # Deleting the owned token state cascades through the association FK, removing both rows
    # atomically before tana-rw switches to a console-held static bearer.
    op.execute(
        sa.text(
            """
            DELETE FROM oauth_token_states
            WHERE token_state_id IN (
                SELECT token_state_id FROM mcp_operator_oauth_associations WHERE server_id = 'tana-rw'
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("ck_oauth_token_states_refresh_failure_shape", "oauth_token_states", type_="check")
    for column in reversed(_FAILURE_COLUMNS):
        op.drop_column("oauth_token_states", column)
