"""A linkage is identified by its configured `server_id` alone; `provider` goes.

`provider` was a closed enum label written on every link and never read back: each configured server
already names itself by `server_id`, and every deployed server's `provider` equalled it. Dropping the
enum lets configuration name any number of servers.

Linkage rows keep their shared OAuth token state in both directions. On the way down the column
comes back as the `server_id`, which is what every deployed row held.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_drop_mcp_linkage_provider"
down_revision = "0016_structured_principals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("mcp_server_linkage", "provider")


def downgrade() -> None:
    op.add_column("mcp_server_linkage", sa.Column("provider", sa.Text(), nullable=True))
    op.execute("UPDATE mcp_server_linkage SET provider = server_id")
    op.alter_column("mcp_server_linkage", "provider", nullable=False)
