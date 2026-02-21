"""Add evaluator_base role with universal read access to all data.

The evaluator role needs read-only access to all ground truth data, agent results,
and metrics across all splits (train, valid, test) without RLS filtering.

Revision ID: 20260220000000
Revises: 20251228000000
Create Date: 2026-02-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260220000000"
down_revision: str | Sequence[str] | None = "20251228000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create evaluator_base role with universal read access."""

    # Create evaluator_base role
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'evaluator_base') THEN
                CREATE ROLE evaluator_base NOLOGIN;
            END IF;
        END
        $$
    """)

    # Grant basic schema access
    op.execute("GRANT USAGE ON SCHEMA public TO evaluator_base")

    # Ground truth tables (read-only)
    ground_truth_tables = [
        "snapshots",
        "true_positives",
        "true_positive_occurrences",
        "false_positives",
        "false_positive_occurrences",
        "occurrence_ranges",
        "false_positive_relevant_files",
        "snapshot_files",
    ]

    for table in ground_truth_tables:
        op.execute(f"GRANT SELECT ON TABLE {table} TO evaluator_base")

    # Agent results tables (read-only)
    agent_result_tables = [
        "agent_runs",
        "reported_issues",
        "reported_issue_occurrences",
        "grading_edges",
        "issue_clusters",
        "issue_cluster_members",
    ]

    for table in agent_result_tables:
        op.execute(f"GRANT SELECT ON TABLE {table} TO evaluator_base")

    # Reference and helper tables (read-only)
    helper_tables = ["file_sets", "file_set_members", "agent_definitions", "llm_requests", "model_metadata"]

    for table in helper_tables:
        op.execute(f"GRANT SELECT ON TABLE {table} TO evaluator_base")

    # Metric and view tables (read-only)
    metric_tables = [
        "grading_pending",
        "clustering_pending",
        "tp_occurrence_credits",
        "recall_by_run",
        "recall_by_definition_example",
        "recall_by_definition_split_kind",
        "recall_by_example",
        "pareto_frontier_by_example",
        "occurrence_statistics",
        "validation_recall_by_definition",
        "agent_run_budget_status",
        "llm_run_costs",
        "llm_request_costs",
    ]

    for table in metric_tables:
        op.execute(f"GRANT SELECT ON TABLE {table} TO evaluator_base")

    # Grant usage on all sequences (for potential INSERT operations)
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO evaluator_base")


def downgrade() -> None:
    """Drop evaluator_base role."""
    op.execute("DROP ROLE IF EXISTS evaluator_base")
