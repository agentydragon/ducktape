"""Record host-side startup failures distinctly from cancellation."""

from alembic import op

revision: str = "20261017000000"
down_revision: str | None = "20260926000000"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE public.agent_run_status_enum ADD VALUE 'startup_failed'")


def downgrade() -> None:
    raise RuntimeError("Cannot remove startup_failed from a PostgreSQL enum without rebuilding dependent schema")
