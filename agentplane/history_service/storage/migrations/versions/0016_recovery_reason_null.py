"""Store an item's absent recovery reason as JSON null rather than an empty string."""

from alembic import op

revision = "0016_recovery_reason_null"
down_revision = "0015_session_opened_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE thread_entity SET state = jsonb_set(state, '{recovery_reason}', 'null'::jsonb) "
        "WHERE entity_kind = 'item' AND state->>'recovery_reason' = ''"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE thread_entity SET state = jsonb_set(state, '{recovery_reason}', '\"\"'::jsonb) "
        "WHERE entity_kind = 'item' AND state->'recovery_reason' = 'null'::jsonb"
    )
