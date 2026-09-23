"""Drop fold state nothing reads: the payload manifest's always-true `present`, and the view
row's `unresolved_count` and `operational.operational_version`.

The view row's models refuse keys they do not declare, so the keys leave the stored rows too.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_drop_unread_fold_state"
down_revision = "0009_thread_entity_index"
branch_labels = None
depends_on = None

_STRIP = """
UPDATE thread_entity
SET state = jsonb_set(state - 'unresolved_count', '{operational}', (state -> 'operational') - 'operational_version')
WHERE entity_kind = 'view_state'
"""

# The count the fold kept is the thread's pending commands; the version restarts from zero.
_RESTORE = """
UPDATE thread_entity AS view
SET state = jsonb_set(
    view.state || jsonb_build_object('unresolved_count', (
        SELECT count(*) FROM thread_entity AS command
        WHERE command.thread_id = view.thread_id
          AND command.projection_epoch = view.projection_epoch
          AND command.entity_kind = 'command'
          AND command.pending
    )),
    '{operational}',
    (view.state -> 'operational') || jsonb_build_object('operational_version', '0')
)
WHERE view.entity_kind = 'view_state'
"""


def upgrade() -> None:
    op.execute(_STRIP)
    op.drop_column("thread_payload_manifest", "present")


def downgrade() -> None:
    op.add_column(
        "thread_payload_manifest", sa.Column("present", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.alter_column("thread_payload_manifest", "present", server_default=None)
    op.execute(_RESTORE)
