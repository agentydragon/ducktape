"""Add evaluator_base role and evaluator login user.

evaluator_base is a NOLOGIN role with BYPASSRLS and SELECT on all tables.
The evaluator login user inherits from evaluator_base, giving it read-only
access to all data while bypassing RLS policies.

Password is taken from PROPS_EVALUATOR_PASSWORD env var at migration time.
ensure_evaluator_role() in setup.py handles password updates on subsequent
deploys (since migrations run only once).

Revision ID: 20260223000000
Revises: 20251228000000
Create Date: 2026-02-23
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "20260223000000"
down_revision: str | Sequence[str] | None = "20251228000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'evaluator_base') THEN
                CREATE ROLE evaluator_base NOLOGIN BYPASSRLS;
            END IF;
        END $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO evaluator_base")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO evaluator_base")
    op.execute("GRANT EXECUTE ON FUNCTION matchable_occurrences(VARCHAR, VARCHAR[]) TO evaluator_base")

    # Create evaluator login user if it doesn't exist.
    # Password from env var at migration time; ensure_evaluator_role() handles updates.
    password = os.environ.get("PROPS_EVALUATOR_PASSWORD")
    conn = op.get_bind()
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'evaluator'")).fetchone()
    if not exists:
        if password:
            conn.execute(text("CREATE ROLE evaluator LOGIN PASSWORD :pw IN ROLE evaluator_base"), {"pw": password})
        else:
            conn.execute(text("CREATE ROLE evaluator LOGIN IN ROLE evaluator_base"))


def downgrade() -> None:
    op.execute("DROP ROLE IF EXISTS evaluator")
    op.execute("DROP ROLE IF EXISTS evaluator_base")
