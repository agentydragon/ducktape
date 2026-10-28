"""Record snapshot-specific Codex titles without changing imported transcripts."""

import sqlalchemy as sa
from alembic import op

revision = "0002_codex_titles"
down_revision = "0001_initial_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions", sa.Column("codex_title_from_metadata", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column(
        "restic_snapshots",
        sa.Column("codex_metadata_status", sa.String(length=20), server_default="pending", nullable=False),
    )
    op.add_column("restic_snapshots", sa.Column("codex_metadata_error", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_restic_snapshots_codex_metadata_status",
        "restic_snapshots",
        "codex_metadata_status IN ('pending', 'complete', 'partial', 'failed')",
    )
    op.create_table(
        "codex_title_observations",
        sa.Column("machine_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("native_session_id", sa.String(), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("title_source", sa.String(length=30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["machine_id", "snapshot_id"],
            ["restic_snapshots.machine_id", "restic_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("machine_id", "snapshot_id", "native_session_id"),
    )
    op.create_index("idx_codex_title_machine_native", "codex_title_observations", ["machine_id", "native_session_id"])


def downgrade() -> None:
    op.drop_index("idx_codex_title_machine_native", table_name="codex_title_observations")
    op.drop_table("codex_title_observations")
    op.drop_constraint("ck_restic_snapshots_codex_metadata_status", "restic_snapshots", type_="check")
    op.drop_column("restic_snapshots", "codex_metadata_error")
    op.drop_column("restic_snapshots", "codex_metadata_status")
    op.drop_column("sessions", "codex_title_from_metadata")
