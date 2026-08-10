"""Drop links.link_profile.

The profile was a snapshot of the intent chosen at Link time, while products_requested is what was
actually asked for. Products grow after linking through update mode, so the two drifted, and the
profile was the field sync used to consult -- see 0004 and the Merrill link, whose profile said
`cashflow` while its products said `investments`. Links are now created from the institution's own
supported product list, so nothing derives behaviour from a profile name any more.

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
    op.drop_column("links", "link_profile")


def downgrade() -> None:
    # The column's information was always derivable from products_requested, but not the label the
    # operator originally picked; 'advanced' is the value meaning "an explicit product list".
    op.add_column("links", sa.Column("link_profile", sa.String(), nullable=False, server_default=sa.text("'advanced'")))
    op.alter_column("links", "link_profile", server_default=None)
