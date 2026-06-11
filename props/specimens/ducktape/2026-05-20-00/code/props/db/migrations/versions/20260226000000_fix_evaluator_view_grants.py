"""Fix evaluator role: add RLS policies and missing view grants.

Two issues with the evaluator role from 20260223000000:

1. BYPASSRLS on evaluator_base is useless — PostgreSQL role attributes like
   BYPASSRLS are NOT inherited via IN ROLE. The evaluator login user never
   had BYPASSRLS, so RLS blocked all access to tables with policies.

2. The materialize_examples migration (20260224000000) dropped and recreated
   8 views granting SELECT only to agent_base, not evaluator_base.

Fix: Add explicit RLS policies granting evaluator full SELECT on all
RLS-enabled tables, grant SELECT on recreated views, and set up ALTER
DEFAULT PRIVILEGES for future migrations.

Revision ID: 20260226000000
Revises: 20260224000000
Create Date: 2026-02-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260226000000"
down_revision: str | None = "20260224000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# All tables with RLS enabled (from 20251228000000_complete_schema.py)
RLS_TABLES = [
    "agent_definitions",
    "agent_runs",
    "snapshots",
    "true_positives",
    "false_positives",
    "true_positive_occurrences",
    "false_positive_occurrences",
    "occurrence_ranges",
    "fp_occurrence_relevant_files",
    "critic_scopes_expected_to_recall",
    "llm_requests",
    "grading_edges",
    "reported_issues",
    "reported_issue_occurrences",
    "file_sets",
    "file_set_members",
    "issue_clusters",
    "issue_cluster_members",
]

# Views recreated by 20260224000000 without evaluator_base grants
VIEWS_TO_GRANT = [
    "examples",
    "tp_occurrence_credits",
    "occurrence_statistics",
    "recall_by_run",
    "recall_by_definition_example",
    "recall_by_definition_split_kind",
    "recall_by_example",
    "pareto_frontier_by_example",
]


def upgrade() -> None:
    # Remove useless BYPASSRLS from evaluator_base (never actually worked for evaluator)
    op.execute("ALTER ROLE evaluator_base NOBYPASSRLS")

    # Add RLS policies granting evaluator full SELECT on all RLS-enabled tables
    for table in RLS_TABLES:
        op.execute(f"CREATE POLICY evaluator_select_all ON {table} FOR SELECT TO evaluator USING (true)")

    # Grant SELECT on views recreated by materialize_examples migration
    for view in VIEWS_TO_GRANT:
        op.execute(f"GRANT SELECT ON TABLE {view} TO evaluator_base")

    # Ensure evaluator_base gets SELECT on tables/views created by future migrations
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO evaluator_base")


def downgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM evaluator_base")

    for view in VIEWS_TO_GRANT:
        op.execute(f"REVOKE SELECT ON TABLE {view} FROM evaluator_base")

    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS evaluator_select_all ON {table}")

    op.execute("ALTER ROLE evaluator_base BYPASSRLS")
