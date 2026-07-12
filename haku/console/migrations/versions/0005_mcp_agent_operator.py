"""Add the agent→operator link table.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_agent_operator",
        sa.Column(
            "agent_dcr_client_id",
            sa.Text(),
            primary_key=True,
            comment="The DCR client_id the OAuth agent presents on /mcp calls (get_access_token().client_id).",
        ),
        sa.Column(
            "operator_subject",
            sa.Text(),
            nullable=False,
            comment=(
                "The authorizing operator's opaque OIDC subject (Authentik sub; sub_mode=user_id -> the user id), "
                "matching the mcp_operator_oauth_associations key so execution resolves the operator token."
            ),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_agent_operator")
