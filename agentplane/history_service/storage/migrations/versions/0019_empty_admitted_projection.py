"""Allow an admitted empty view before its first Event establishes the source."""

from alembic import op

revision = "0019_empty_admitted_projection"
down_revision = "0018_summary_idle_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE thread_checkpoint ALTER COLUMN source_id DROP NOT NULL")
    op.create_check_constraint(
        "thread_checkpoint_source_prefix", "thread_checkpoint", "source_id IS NOT NULL OR through_cursor = 0"
    )


def downgrade() -> None:
    # Refuse downgrade while source-less admitted views exist; never delete their state.
    op.execute("ALTER TABLE thread_checkpoint ALTER COLUMN source_id SET NOT NULL")
    op.drop_constraint("thread_checkpoint_source_prefix", "thread_checkpoint", type_="check")
