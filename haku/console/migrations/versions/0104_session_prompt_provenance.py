"""Record prompt digest provenance on sessions.

Prompts moved to Agents (#4431 stage 6): each session records the SHA-256 of the composed
identity+fragment template source it was launched with and of the exact rendered appended prompt,
stamped once at the first successful render. Both columns are NULL together — a session that never
rendered an appended prompt, including every session predating these columns.

Revision ID: 0104
Revises: 0103
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0104"
down_revision: str | None = "0103"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("system_prompt_template_digest", sa.LargeBinary(), nullable=True))
    op.add_column("sessions", sa.Column("system_prompt_rendered_digest", sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        "ck_sessions_prompt_digest_pair",
        "sessions",
        "(system_prompt_template_digest IS NULL) = (system_prompt_rendered_digest IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sessions_prompt_digest_pair", "sessions", type_="check")
    op.drop_column("sessions", "system_prompt_rendered_digest")
    op.drop_column("sessions", "system_prompt_template_digest")
