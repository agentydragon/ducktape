"""Project session kind from captured harness metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0003_session_kind"
down_revision = "0002_codex_titles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("session_kind", sa.String(length=20), nullable=True))
    op.create_check_constraint(
        "ck_sessions_session_kind",
        "sessions",
        "session_kind IS NULL OR session_kind IN ('main', 'subagent', 'guardian', 'unknown')",
    )
    op.create_index(
        "idx_sessions_machine_harness_kind_activity",
        "sessions",
        ["machine_id", "harness", "session_kind", "last_activity_at", "id"],
    )
    op.execute(
        "UPDATE sessions SET session_kind = 'subagent' "
        "WHERE harness = 'claude' AND parent_link_kind = 'claude_path_inferred'"
    )

    op.add_column("codex_title_observations", sa.Column("session_kind", sa.String(length=20), nullable=True))
    op.create_check_constraint(
        "ck_codex_title_observations_session_kind",
        "codex_title_observations",
        "session_kind IS NULL OR session_kind IN ('main', 'subagent', 'guardian', 'unknown')",
    )
    op.alter_column("codex_title_observations", "title", existing_type=sa.String(), nullable=True)
    op.alter_column("codex_title_observations", "title_source", existing_type=sa.String(length=30), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM codex_title_observations WHERE title IS NULL OR title_source IS NULL")
    op.alter_column("codex_title_observations", "title_source", existing_type=sa.String(length=30), nullable=False)
    op.alter_column("codex_title_observations", "title", existing_type=sa.String(), nullable=False)
    op.drop_constraint("ck_codex_title_observations_session_kind", "codex_title_observations", type_="check")
    op.drop_column("codex_title_observations", "session_kind")
    op.drop_index("idx_sessions_machine_harness_kind_activity", table_name="sessions")
    op.drop_constraint("ck_sessions_session_kind", "sessions", type_="check")
    op.drop_column("sessions", "session_kind")
