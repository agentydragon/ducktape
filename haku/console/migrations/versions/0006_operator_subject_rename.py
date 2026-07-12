"""Rename `operator_principal` → `operator_subject` on the operator OAuth tables.

The column holds the operator's opaque OIDC subject (Authentik `sub`, `sub_mode=user_id`), not an
invented "principal" — name it for what it is, matching `mcp_agent_operator.operator_subject`.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("mcp_operator_oauth_associations", "operator_principal", new_column_name="operator_subject")
    op.alter_column("mcp_operator_oauth_flows", "operator_principal", new_column_name="operator_subject")


def downgrade() -> None:
    op.alter_column("mcp_operator_oauth_associations", "operator_subject", new_column_name="operator_principal")
    op.alter_column("mcp_operator_oauth_flows", "operator_subject", new_column_name="operator_principal")
