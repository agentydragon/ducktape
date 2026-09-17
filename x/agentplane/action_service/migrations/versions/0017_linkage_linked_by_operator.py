"""Record who linked an MCP server as an issuer and a subject, not as `<issuer>:<subject>`.

The linkage rows themselves carry the OAuth token state, so they stay: `linked_by` is nullable
provenance and only it is dropped. Existing links keep working and lose the name of whoever made
them, which is not worth parsing a formatted string back apart to keep.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_linkage_linked_by_operator"
down_revision = "0016_structured_principals"
branch_labels = None
depends_on = None

_WHOLE = "(linked_by_issuer IS NULL) = (linked_by_subject IS NULL)"


def upgrade() -> None:
    op.drop_column("mcp_server_linkage", "linked_by")
    op.add_column("mcp_server_linkage", sa.Column("linked_by_issuer", sa.Text(), nullable=True))
    op.add_column("mcp_server_linkage", sa.Column("linked_by_subject", sa.Text(), nullable=True))
    op.create_check_constraint("mcp_server_linkage_linked_by_whole", "mcp_server_linkage", _WHOLE)


def downgrade() -> None:
    op.drop_constraint("mcp_server_linkage_linked_by_whole", "mcp_server_linkage", type_="check")
    op.drop_column("mcp_server_linkage", "linked_by_issuer")
    op.drop_column("mcp_server_linkage", "linked_by_subject")
    op.add_column("mcp_server_linkage", sa.Column("linked_by", sa.Text(), nullable=True))
