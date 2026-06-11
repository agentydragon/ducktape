"""Materialize the examples view for performance.

The examples VIEW computes recall_denominator via is_tp_in_expected_recall_scope(),
a PL/pgSQL function called O(thousands) of times per scan. Downstream views
(recall_by_run, tp_occurrence_credits) inline the scan multiple times, causing
~38s query times on small datasets. Converting to a MATERIALIZED VIEW eliminates
repeated function calls — the data changes only when specimens are synced.

Revision ID: 20260224000000
Revises: 20260223000000
Create Date: 2026-02-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260224000000"
down_revision: str | None = "20260223000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# All views that depend on `examples`, in dependency order.
# DROP VIEW examples CASCADE drops all of these.
DEPENDENT_VIEWS_GRANTS = [
    "tp_occurrence_credits",
    "occurrence_statistics",
    "recall_by_run",
    "recall_by_definition_example",
    "recall_by_definition_split_kind",
    "recall_by_example",
    "pareto_frontier_by_example",
]

EXAMPLES_SQL = """
    -- Whole-snapshot examples (one per snapshot)
    SELECT
        s.slug AS snapshot_slug,
        'whole_snapshot'::example_kind_enum AS example_kind,
        NULL::text AS files_hash,
        COALESCE((
            SELECT COUNT(DISTINCT (tpo.tp_id, tpo.occurrence_id))
            FROM true_positive_occurrences tpo
            JOIN true_positives t ON tpo.snapshot_slug = t.snapshot_slug AND tpo.tp_id = t.tp_id
            WHERE t.snapshot_slug = s.slug
        ), 0)::integer AS recall_denominator
    FROM snapshots s

    UNION ALL

    -- File-set examples (one per unique file set)
    SELECT
        fs.snapshot_slug,
        'file_set'::example_kind_enum AS example_kind,
        fs.files_hash,
        COALESCE((
            SELECT COUNT(DISTINCT (tpo.tp_id, tpo.occurrence_id))
            FROM true_positive_occurrences tpo
            JOIN true_positives t ON tpo.snapshot_slug = t.snapshot_slug AND tpo.tp_id = t.tp_id
            WHERE t.snapshot_slug = fs.snapshot_slug
              AND is_tp_in_expected_recall_scope(fs.snapshot_slug, t.tp_id, tpo.occurrence_id, 'file_set'::example_kind_enum, fs.files_hash)
        ), 0)::integer AS recall_denominator
    FROM file_sets fs
"""

TP_OCCURRENCE_CREDITS_SQL = """
    CREATE VIEW tp_occurrence_credits AS
    SELECT
        (cr.type_config->'example'->>'snapshot_slug') AS snapshot_slug,
        s.split,
        ex.example_kind,
        ex.files_hash,
        tpo.tp_id,
        tpo.occurrence_id,
        cr.agent_run_id AS critic_run_id,
        cr.image_digest AS critic_image_digest,
        cr.model AS critic_model,
        COALESCE(SUM(ge.credit), 0.0) AS found_credit
    FROM agent_runs cr
    JOIN snapshots s ON (cr.type_config->'example'->>'snapshot_slug') = s.slug
    JOIN examples ex ON (
        (cr.type_config->'example'->>'snapshot_slug') = ex.snapshot_slug
        AND (cr.type_config->'example'->>'kind')::example_kind_enum = ex.example_kind
        AND COALESCE((cr.type_config->'example'->>'files_hash'), '') = COALESCE(ex.files_hash, '')
    )
    CROSS JOIN true_positive_occurrences tpo
    LEFT JOIN grading_edges ge ON (
        ge.critique_run_id = cr.agent_run_id
        AND ge.snapshot_slug = tpo.snapshot_slug
        AND ge.tp_id = tpo.tp_id
        AND ge.tp_occurrence_id = tpo.occurrence_id
    )
    WHERE (cr.type_config->>'agent_type') = 'critic'
      AND (cr.type_config->'example'->>'snapshot_slug') = tpo.snapshot_slug
      AND is_tp_in_expected_recall_scope(tpo.snapshot_slug, tpo.tp_id, tpo.occurrence_id, ex.example_kind, ex.files_hash)
    GROUP BY cr.agent_run_id, s.split, ex.example_kind, ex.files_hash,
             tpo.snapshot_slug, tpo.tp_id, tpo.occurrence_id,
             cr.image_digest, cr.model
"""

OCCURRENCE_STATISTICS_SQL = """
    CREATE VIEW occurrence_statistics AS
    SELECT
        snapshot_slug,
        split,
        example_kind,
        files_hash,
        tp_id,
        occurrence_id,
        critic_image_digest,
        critic_model,
        compute_stats_with_ci(array_agg(found_credit)) AS credit_stats
    FROM tp_occurrence_credits
    GROUP BY snapshot_slug, split, example_kind, files_hash, tp_id, occurrence_id,
        critic_image_digest, critic_model
"""

RECALL_BY_RUN_SQL = """
    CREATE VIEW recall_by_run AS
    WITH per_run AS (
        SELECT
            cr.type_config->'example'->>'snapshot_slug' AS snapshot_slug,
            e.example_kind,
            e.files_hash,
            s.split,
            e.recall_denominator,
            cr.agent_run_id AS critic_run_id,
            cr.image_digest AS critic_image_digest,
            cr.model AS critic_model,
            cr.status AS critic_status,
            COALESCE((
                SELECT SUM(toc.found_credit)
                FROM tp_occurrence_credits toc
                WHERE toc.critic_run_id = cr.agent_run_id
            ), 0.0) AS total_credit,
            (
                SELECT COUNT(*) FROM grading_pending gp
                WHERE gp.critique_run_id = cr.agent_run_id
            ) AS missing_grading_edges
        FROM agent_runs cr
        JOIN examples e ON (
            cr.type_config->'example'->>'snapshot_slug' = e.snapshot_slug
            AND (cr.type_config->'example'->>'kind')::example_kind_enum = e.example_kind
            AND COALESCE((cr.type_config->'example'->>'files_hash'), '') = COALESCE(e.files_hash, '')
        )
        JOIN snapshots s ON cr.type_config->'example'->>'snapshot_slug' = s.slug
        WHERE (cr.type_config->>'agent_type') = 'critic'
    )
    SELECT
        snapshot_slug, example_kind, files_hash, split, recall_denominator,
        critic_run_id, critic_image_digest, critic_model, critic_status,
        total_credit,
        CASE WHEN recall_denominator > 0
            THEN total_credit / recall_denominator
            ELSE 0.0
        END AS recall,
        missing_grading_edges
    FROM per_run
"""

RECALL_BY_DEFINITION_EXAMPLE_SQL = """
    CREATE VIEW recall_by_definition_example AS
    WITH raw_stats AS (
        SELECT
            rbr.critic_image_digest,
            rbr.critic_model,
            rbr.snapshot_slug,
            rbr.example_kind,
            rbr.files_hash,
            rbr.split,
            MAX(rbr.recall_denominator)::integer AS recall_denominator,
            COUNT(*)::integer AS n_runs,
            agg_status_counts(array_agg(rbr.critic_status)) AS status_counts,
            compute_stats_with_ci(array_agg(
                rbr.total_credit
            )) AS credit_stats
        FROM recall_by_run rbr
        GROUP BY rbr.critic_image_digest, rbr.critic_model,
                 rbr.snapshot_slug, rbr.example_kind, rbr.files_hash, rbr.split
    )
    SELECT
        critic_image_digest, critic_model,
        snapshot_slug, example_kind, files_hash, split,
        recall_denominator, n_runs, status_counts, credit_stats,
        scale_stats(credit_stats, recall_denominator) AS recall_stats
    FROM raw_stats
"""

RECALL_BY_DEFINITION_SPLIT_KIND_SQL = """
    CREATE VIEW recall_by_definition_split_kind AS
    WITH
    example_counts AS (
        SELECT
            split, example_kind, critic_image_digest, critic_model,
            COUNT(*)::integer AS n_examples,
            SUM(recall_denominator)::integer AS recall_denominator
        FROM (
            SELECT DISTINCT
                split, example_kind, files_hash, recall_denominator,
                critic_image_digest, critic_model
            FROM recall_by_definition_example
        ) per_example
        GROUP BY split, example_kind, critic_image_digest, critic_model
    ),
    run_stats AS (
        SELECT
            split, example_kind, critic_image_digest, critic_model,
            COUNT(*)::integer AS n_runs,
            agg_status_counts(array_agg(status_counts)) AS status_counts,
            compute_stats_with_ci(array_agg(
                COALESCE((credit_stats).mean, 0.0)
            )) AS credit_stats,
            COUNT(*) FILTER (WHERE COALESCE((credit_stats).mean, 0.0) = 0.0)::integer AS zero_count
        FROM recall_by_definition_example
        GROUP BY split, example_kind, critic_image_digest, critic_model
    )
    SELECT
        rs.split, rs.example_kind, rs.critic_image_digest, rs.critic_model,
        ec.n_examples, rs.n_runs, ec.recall_denominator,
        rs.status_counts, rs.credit_stats,
        scale_stats(rs.credit_stats, ec.recall_denominator) AS recall_stats,
        rs.zero_count
    FROM run_stats rs
    JOIN example_counts ec USING (split, example_kind, critic_image_digest, critic_model)
"""

RECALL_BY_EXAMPLE_SQL = """
    CREATE VIEW recall_by_example AS
    WITH raw_stats AS (
        SELECT
            rbde.snapshot_slug,
            rbde.example_kind,
            rbde.files_hash,
            rbde.split,
            MAX(rbde.recall_denominator)::integer AS recall_denominator,
            rbde.critic_model,
            SUM(rbde.n_runs)::integer AS n_runs,
            agg_status_counts(array_agg(rbde.status_counts)) AS status_counts,
            compute_stats_with_ci(array_agg(
                COALESCE((rbde.credit_stats).mean, 0.0)
            )) AS credit_stats
        FROM recall_by_definition_example rbde
        GROUP BY rbde.snapshot_slug, rbde.example_kind, rbde.files_hash, rbde.split, rbde.critic_model
    )
    SELECT
        snapshot_slug, example_kind, files_hash, split,
        recall_denominator, critic_model, n_runs, status_counts, credit_stats,
        scale_stats(credit_stats, recall_denominator) AS recall_stats
    FROM raw_stats
"""

PARETO_FRONTIER_BY_EXAMPLE_SQL = """
    CREATE VIEW pareto_frontier_by_example AS
    WITH best_scores AS (
        SELECT
            snapshot_slug,
            example_kind,
            files_hash,
            split,
            MAX(recall_denominator) AS recall_denominator,
            critic_model,
            MAX(COALESCE((credit_stats).mean, 0.0)) AS best_mean_credit
        FROM recall_by_definition_example
        GROUP BY snapshot_slug, example_kind, files_hash, split, critic_model
    ),
    ranked AS (
        SELECT
            rbde.*,
            (rbde.credit_stats).mean AS mean_credit,
            bs.best_mean_credit
        FROM recall_by_definition_example rbde
        JOIN best_scores bs USING (snapshot_slug, example_kind, files_hash, split, critic_model)
        WHERE COALESCE((rbde.credit_stats).mean, 0.0) = bs.best_mean_credit
    )
    SELECT
        snapshot_slug, example_kind, files_hash, split,
        MAX(recall_denominator)::integer AS recall_denominator,
        critic_model,
        jsonb_agg(DISTINCT jsonb_build_object(
            'image_digest', critic_image_digest,
            'credit_stats', credit_stats,
            'n_runs', n_runs
        )) AS winning_definitions,
        best_mean_credit
    FROM ranked
    GROUP BY snapshot_slug, example_kind, files_hash, split, critic_model, best_mean_credit
"""


def upgrade() -> None:
    # PG 18 (commit 4b74ebf726) changed CREATE MATERIALIZED VIEW ... WITH DATA to
    # use the REFRESH code path, which calls RestrictSearchPath() — forcing
    # search_path to 'pg_catalog, pg_temp'. When the planner then tries to inline
    # a LANGUAGE sql function, it re-parses the function body text. Unqualified
    # table references (e.g. 'critic_scopes_expected_to_recall') fail to resolve
    # because 'public' is no longer in the search_path.
    #
    # Adding SET search_path = public to the function prevents inlining entirely
    # (the optimizer skips functions with proconfig set), so the function executes
    # normally with a correct search_path regardless of caller context.
    op.execute("""
        ALTER FUNCTION is_tp_in_expected_recall_scope(text, text, text, example_kind_enum, text)
        SET search_path = public
    """)

    # Drop the examples VIEW and all dependents via CASCADE
    op.execute("DROP VIEW IF EXISTS examples CASCADE")

    # Recreate as MATERIALIZED VIEW (populated on creation)
    op.execute(f"CREATE MATERIALIZED VIEW examples AS {EXAMPLES_SQL}")

    # Recreate all dependent views in dependency order
    op.execute(TP_OCCURRENCE_CREDITS_SQL)
    op.execute(OCCURRENCE_STATISTICS_SQL)
    op.execute(RECALL_BY_RUN_SQL)
    op.execute(RECALL_BY_DEFINITION_EXAMPLE_SQL)
    op.execute(RECALL_BY_DEFINITION_SPLIT_KIND_SQL)
    op.execute(RECALL_BY_EXAMPLE_SQL)
    op.execute(PARETO_FRONTIER_BY_EXAMPLE_SQL)

    # Re-grant SELECT to agent_base on examples (now matview) and all recreated views
    op.execute("GRANT SELECT ON TABLE examples TO agent_base")
    for view in DEPENDENT_VIEWS_GRANTS:
        op.execute(f"GRANT SELECT ON TABLE {view} TO agent_base")


def downgrade() -> None:
    # Drop the materialized view and all dependents
    op.execute("DROP MATERIALIZED VIEW IF EXISTS examples CASCADE")

    # Remove the SET search_path added in upgrade (restore original function behavior)
    op.execute("""
        ALTER FUNCTION is_tp_in_expected_recall_scope(text, text, text, example_kind_enum, text)
        RESET search_path
    """)

    # Recreate as regular VIEW
    op.execute(f"CREATE VIEW examples AS {EXAMPLES_SQL}")

    # Recreate all dependent views in dependency order
    op.execute(TP_OCCURRENCE_CREDITS_SQL)
    op.execute(OCCURRENCE_STATISTICS_SQL)
    op.execute(RECALL_BY_RUN_SQL)
    op.execute(RECALL_BY_DEFINITION_EXAMPLE_SQL)
    op.execute(RECALL_BY_DEFINITION_SPLIT_KIND_SQL)
    op.execute(RECALL_BY_EXAMPLE_SQL)
    op.execute(PARETO_FRONTIER_BY_EXAMPLE_SQL)

    # Re-grant SELECT
    op.execute("GRANT SELECT ON TABLE examples TO agent_base")
    for view in DEPENDENT_VIEWS_GRANTS:
        op.execute(f"GRANT SELECT ON TABLE {view} TO agent_base")
