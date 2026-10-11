"""Drop the app's retired projection: the fold tables, which nothing has read or written since the
app read Threads from the History Service. The History Service keeps its own copy, rebuilt from the
raw log."""

from alembic import op

revision = "0032_app_projection_drop"
down_revision = "0031_app_electric_retire"
branch_labels = None
depends_on = None

FOLD_TABLES = (
    "session_projection_lease",
    "thread_checkpoint",
    "thread_history_summary",
    "thread_entity",
    "thread_payload_manifest",
    "thread_payload_chunk",
    "thread_evidence",
    "thread_native_link",
)


def upgrade() -> None:
    # No CASCADE: a dependency outside the fold must stop the rollout, not be erased.
    op.execute(f"DROP TABLE {', '.join(FOLD_TABLES)}")


def downgrade() -> None:
    raise RuntimeError("the app projection is gone; the History Service holds the fold")
