"""Runner connection ownership moved to a Lease per Sandbox; the row keeps only agreed operations."""

import sqlalchemy as sa
from alembic import op

revision = "0006_connection_lease"
down_revision = "0005_pending_submission_index"
branch_labels = None
depends_on = None

_UNREAD = {
    "pod_uid": sa.String(),
    "connection_epoch": sa.BigInteger(),
    "owner_replica": sa.String(),
    "runner_boot_id": sa.String(),
    "last_seen_at": sa.DateTime(timezone=True),
}


def upgrade() -> None:
    for column, type_ in _UNREAD.items():
        op.alter_column("runner_connection", column, existing_type=type_, nullable=True)


def downgrade() -> None:
    # Keep the agreed operations, which keep a Sandbox off the inbound route; the placeholder
    # connection reads as long dead.
    op.execute(
        "UPDATE runner_connection SET pod_uid = '', connection_epoch = 1, owner_replica = '', "
        "runner_boot_id = '', last_seen_at = 'epoch' WHERE connection_epoch IS NULL"
    )
    for column, type_ in _UNREAD.items():
        op.alter_column("runner_connection", column, existing_type=type_, nullable=False)
