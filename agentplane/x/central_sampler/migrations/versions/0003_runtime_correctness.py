"""Scope turn interrupts and retain independently drained process output."""

from alembic import op

revision = "0003_runtime_correctness"
down_revision = "0002_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE spike_interrupt ADD COLUMN target_turn VARCHAR")
    op.execute("ALTER TABLE spike_process ADD COLUMN buffered_output BYTEA NOT NULL DEFAULT ''::bytea")


def downgrade() -> None:
    raise RuntimeError("Runtime evidence is durable; restore a backup instead of discarding it")
