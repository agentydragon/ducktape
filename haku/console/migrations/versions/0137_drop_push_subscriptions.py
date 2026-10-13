"""Drop the retired Console Web Push subscription table.

No Console image since the Web Push removal maps `push_subscriptions`, so the previous image the
rolling deploy overlapped with is gone. The index and the operator foreign key go with the table.

Revision ID: 0137
Revises: 0136
"""

from alembic import op

revision: str = "0137"
down_revision: str | None = "0136"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("push_subscriptions")


def downgrade() -> None:
    raise RuntimeError("0137 deleted the Console Web Push subscriptions; they cannot be restored")
