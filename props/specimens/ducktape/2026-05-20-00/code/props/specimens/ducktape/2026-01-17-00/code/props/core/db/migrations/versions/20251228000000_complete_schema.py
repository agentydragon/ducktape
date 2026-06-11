"""Squashed schema for props database.

This is a complete schema migration that replaces all previous migrations.
Incorporates:
- Unified agent_runs table (replaces legacy critic_runs, grader_runs, etc.)
- stats_with_ci composite type for statistics with 95% confidence intervals
- Occurrence-weighted aggregation (raw totals, not normalized ratios)
- RLS policies for agent data isolation
- SECURITY DEFINER functions for RLS bypasses
- Optimized examples view with array containment
- in_progress filter in recall views
- graders_match_only_if_reported_on for sparse grading
- No clustering tables (deprecated feature removed)
- Normalized occurrence ranges (replaces JSONB files/relevant_files columns)

Revision ID: 20251228000000
Revises: None
Create Date: 2025-12-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20251228000000"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Issue ID format constraint (matches props.ids.BaseIssueID):
# - lowercase alphanumeric, underscore, hyphen only
# - 5-40 characters
# - no colons (implicit from pattern)
ISSUE_ID_CHECK_SQL = "~ '^[a-z0-9_-]+$' AND length({col}) >= 5 AND length({col}) <= 40"


def issue_id_constraint(column: str, name: str) -> sa.CheckConstraint:
    """Create a CHECK constraint enforcing BaseIssueID format on a column."""
    return sa.CheckConstraint(f"{column} {ISSUE_ID_CHECK_SQL.format(col=column)}", name=name)


def upgrade() -> None:
    """Create complete schema.

    Order:
    1. Extensions
    2. ENUMs
    3. Composite types
    4. Functions (all - body checking disabled to allow forward references)
    5. Tables
    6. Indexes
    7. Views (including grading_credit_sums needed by trigger)
    8. Triggers
    9. Roles and grants
    10. RLS policies
    """

    # Disable function body checking to allow functions to reference tables not yet created
    # (same as pg_dump does - references are validated at execution time instead)
    op.execute("SET check_function_bodies = false")

    # =========================================================================
    # 1. Extensions
    # =========================================================================
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")

    # =========================================================================
    # 2. ENUMs
    # =========================================================================
    op.execute("""
        CREATE TYPE agent_run_status_enum AS ENUM (
            'in_progress',
            'completed',
            'max_turns_exceeded',
            'context_length_exceeded',
            'reported_failure'
        )
    """)

    # Note: 'clustering' value kept for backward compatibility but is deprecated/unused
    op.execute("""
        CREATE TYPE agent_type_enum AS ENUM (
            'critic',
            'grader',
            'prompt_optimizer',
            'clustering',
            'freeform',
            'improvement'
        )
    """)

    op.execute("""
        COMMENT ON TYPE agent_type_enum IS
        'Agent types. Note: clustering value is deprecated and unused.'
    """)

    op.execute("""
        CREATE TYPE split_enum AS ENUM (
            'train',
            'valid',
            'test'
        )
    """)

    op.execute("""
        CREATE TYPE example_kind_enum AS ENUM (
            'whole_snapshot',
            'file_set'
        )
    """)

    # =========================================================================
    # 3. Composite types
    # =========================================================================
    op.execute("""
        CREATE TYPE stats_with_ci AS (
            n integer,
            mean double precision,
            min double precision,
            max double precision,
            lcb95 double precision,
            ucb95 double precision
        )
    """)

    op.execute("""
        COMMENT ON TYPE stats_with_ci IS
        'Statistics with 95% confidence interval bounds. Used for aggregated metrics.
- n: sample count
- mean: sample mean
- min: minimum value
- max: maximum value
- lcb95: lower 95% confidence bound (mean - 1.96 * stddev/sqrt(n))
- ucb95: upper 95% confidence bound (mean + 1.96 * stddev/sqrt(n))
Returns NULL for lcb95/ucb95 when n < 2 (insufficient samples for CI).'
    """)

    # =========================================================================
    # 4. Functions (table-independent - can be created before tables)
    # =========================================================================

    # Helper: aggregate status counts into JSONB
    op.execute("""
        CREATE FUNCTION agg_status_counts(statuses agent_run_status_enum[])
        RETURNS jsonb
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT jsonb_object_agg(s, cnt)
            FROM (
                SELECT s, count(*) AS cnt
                FROM unnest(statuses) AS s
                GROUP BY s
            ) sub
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION agg_status_counts(agent_run_status_enum[]) IS
        'Aggregates an array of status values into JSONB counts. Used by aggregate views.
Example: agg_status_counts(array_agg(status)) -> {"completed": 5, "max_turns_exceeded": 2}'
    """)

    # Helper: merge array of status count JSONBs (for re-aggregation)
    op.execute("""
        CREATE FUNCTION agg_status_counts(counts jsonb[])
        RETURNS jsonb
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT COALESCE(
                jsonb_object_agg(key, total),
                '{}'::jsonb
            )
            FROM (
                SELECT key, SUM(value::bigint) AS total
                FROM unnest(counts) AS c,
                     jsonb_each_text(c) AS kv(key, value)
                GROUP BY key
            ) sub
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION agg_status_counts(jsonb[]) IS
        'Merges an array of status count JSONBs by summing counts per status. Used by higher-level views.
Example: agg_status_counts(ARRAY[''{"completed": 2}'', ''{"completed": 3}'']::jsonb[]) -> {"completed": 5}'
    """)

    # Helper: compute statistics with confidence intervals
    op.execute("""
        CREATE FUNCTION compute_stats_with_ci(vals double precision[])
        RETURNS stats_with_ci
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT ROW(
                count(*)::integer,
                avg(v),
                min(v),
                max(v),
                CASE WHEN count(*) > 1 THEN avg(v) - 1.96 * stddev_samp(v) / sqrt(count(*)) ELSE NULL END,
                CASE WHEN count(*) > 1 THEN avg(v) + 1.96 * stddev_samp(v) / sqrt(count(*)) ELSE NULL END
            )::stats_with_ci
            FROM unnest(vals) AS v
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION compute_stats_with_ci(double precision[]) IS
        'Computes n, mean, min, max, and 95% confidence bounds from an array of values.
Usage: compute_stats_with_ci(array_agg(some_metric))
Access fields: (compute_stats_with_ci(...)).mean, .min, .max, .lcb95, .ucb95, etc.'
    """)

    # Helper: scale stats_with_ci by a divisor (e.g., credit -> recall)
    op.execute("""
        CREATE FUNCTION scale_stats(s stats_with_ci, divisor double precision)
        RETURNS stats_with_ci
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT CASE WHEN divisor = 0 THEN
                ROW(s.n, 0.0, 0.0, 0.0, NULL, NULL)::stats_with_ci
            ELSE
                ROW(
                    s.n,
                    s.mean / divisor,
                    s.min / divisor,
                    s.max / divisor,
                    s.lcb95 / divisor,
                    s.ucb95 / divisor
                )::stats_with_ci
            END
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION scale_stats(stats_with_ci, double precision) IS
        'Divides all values in a stats_with_ci by a divisor.
Use to convert raw count stats to ratio stats (e.g., credit / recall_denominator for recall).
Example: scale_stats(credit_stats, recall_denominator)'
    """)

    # Helper: current_agent_run_id from session username
    op.execute("""
        CREATE FUNCTION current_agent_run_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$
            SELECT CASE
                WHEN session_user LIKE 'agent_%'
                THEN substring(session_user from 'agent_([0-9a-f-]+)')::uuid
                ELSE NULL
            END
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION current_agent_run_id() IS
        'Extract agent_run_id from session username (NULL if not an agent).
Uses session_user (not current_user) to work correctly when called from within SECURITY DEFINER functions.'
    """)

    # Helper: get_agent_type_config (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION get_agent_type_config(p_agent_run_id uuid) RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT type_config
            FROM agent_runs
            WHERE agent_run_id = p_agent_run_id
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION get_agent_type_config(uuid) IS
        'Returns type_config JSONB for given agent_run_id. SECURITY DEFINER to bypass RLS on agent_runs.'
    """)

    # Helper: current_agent_type_config (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION current_agent_type_config() RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT type_config
            FROM agent_runs
            WHERE agent_run_id = current_agent_run_id()
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION current_agent_type_config() IS
        'Returns type_config JSONB for current agent. SECURITY DEFINER to bypass RLS on agent_runs.
Returns NULL for non-agents.'
    """)

    # Helper: current_agent_type (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION current_agent_type() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT current_agent_type_config()->>'agent_type'
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION current_agent_type() IS
        'Returns agent_type from current_agent_type_config(). SECURITY DEFINER for RLS policy use.'
    """)

    # Helper: get_graded_agent_run_id (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION get_graded_agent_run_id(p_grader_run_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT (type_config->>'graded_agent_run_id')::UUID
            FROM agent_runs
            WHERE agent_run_id = p_grader_run_id
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION get_graded_agent_run_id(uuid) IS
        'Returns graded_agent_run_id from grader type_config. SECURITY DEFINER to bypass RLS.'
    """)

    # Helper: current_graded_agent_run_id (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION current_graded_agent_run_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT get_graded_agent_run_id(current_agent_run_id())
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION current_graded_agent_run_id() IS
        'Returns graded_agent_run_id from current grader type_config. SECURITY DEFINER to bypass RLS.'
    """)

    # Helper: get_graded_snapshot_slug (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION get_graded_snapshot_slug(grader_run_id uuid) RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT type_config->'example'->>'snapshot_slug'
            FROM agent_runs
            WHERE agent_run_id = get_graded_agent_run_id(grader_run_id)
        $$
    """)

    # Helper: derive_agent_password (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION derive_agent_password(run_id uuid) RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT encode(
                sha256((SELECT salt FROM agent_role_salt) || run_id::text::bytea),
                'hex'
            )
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION derive_agent_password(uuid) IS
        'Derive deterministic password for agent role (admin-only)'
    """)

    # Helper: create_agent_role (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION create_agent_role(run_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        AS $$
        DECLARE
            username TEXT := 'agent_' || run_id::text;
            password TEXT := derive_agent_password(run_id);
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = username) THEN
                EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', username, password);
                EXECUTE format('GRANT agent_base TO %I', username);
            END IF;
        END
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION create_agent_role(uuid) IS
        'Create LOGIN role for agent with deterministic password (admin-only)'
    """)

    # Snapshot predicate functions
    op.execute("""
        CREATE FUNCTION is_train_snapshot(slug text) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM snapshots
                WHERE snapshots.slug = is_train_snapshot.slug AND split = 'train'
            )
        $$
    """)

    op.execute("""
        CREATE FUNCTION is_valid_snapshot(slug text) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM snapshots
                WHERE snapshots.slug = is_valid_snapshot.slug AND split = 'valid'
            )
        $$
    """)

    op.execute("""
        CREATE FUNCTION is_train_or_valid_snapshot(slug text) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM snapshots
                WHERE snapshots.slug = is_train_or_valid_snapshot.slug
                  AND split IN ('train', 'valid')
            )
        $$
    """)

    # Helper: is_train_agent_run (SECURITY DEFINER)
    op.execute("""
        CREATE FUNCTION is_train_agent_run(run_id uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT COALESCE(
                CASE get_agent_type_config(run_id)->>'agent_type'
                    WHEN 'critic' THEN is_train_snapshot(get_agent_type_config(run_id)->'example'->>'snapshot_slug')
                    WHEN 'grader' THEN is_train_snapshot(get_graded_snapshot_slug(run_id))
                    ELSE FALSE
                END,
                FALSE
            )
        $$
    """)

    # Improvement agent helpers
    op.execute("""
        CREATE FUNCTION is_improvement_example_allowed(
            p_snapshot_slug text,
            p_example_kind example_kind_enum,
            p_files_hash text
        ) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            SELECT COALESCE(
                (current_agent_type_config()->>'agent_type' = 'improvement')
                AND EXISTS (
                    SELECT 1 FROM jsonb_array_elements(current_agent_type_config()->'allowed_examples') elem
                    WHERE elem->>'snapshot_slug' = p_snapshot_slug
                      AND (elem->>'kind')::example_kind_enum = p_example_kind
                      AND (
                          -- NULL files_hash for whole_snapshot examples
                          (p_example_kind = 'whole_snapshot' AND (elem->>'files_hash') IS NULL)
                          OR (p_example_kind = 'file_set' AND (elem->>'files_hash') = p_files_hash)
                      )
                ),
                FALSE
            )
        $$
    """)

    op.execute("""
        CREATE FUNCTION is_improvement_snapshot_allowed(p_slug text) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            SELECT COALESCE(
                (current_agent_type_config()->>'agent_type' = 'improvement')
                AND EXISTS (
                    SELECT 1 FROM jsonb_array_elements(current_agent_type_config()->'allowed_examples') elem
                    WHERE elem->>'snapshot_slug' = p_slug
                ),
                FALSE
            )
        $$
    """)

    op.execute("""
        CREATE FUNCTION get_improvement_allowed_agent_run_ids() RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT ar.agent_run_id
            FROM agent_runs ar
            WHERE current_agent_type_config()->>'agent_type' = 'improvement'
              AND ar.type_config->>'agent_type' IN ('critic', 'grader')
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements(current_agent_type_config()->'allowed_examples') elem
                  WHERE elem->>'snapshot_slug' = ar.type_config->'example'->>'snapshot_slug'
                    AND elem->>'kind' = ar.type_config->'example'->>'kind'
                    AND (
                        (ar.type_config->'example'->>'kind' = 'whole_snapshot' AND (elem->>'files_hash') IS NULL)
                        OR (ar.type_config->'example'->>'kind' = 'file_set' AND (elem->>'files_hash') = (ar.type_config->'example'->>'files_hash'))
                    )
              )
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION get_improvement_allowed_agent_run_ids() IS
        'Returns agent_run_ids for critic/grader runs that match current improvement agent allowed_examples.
SECURITY DEFINER to bypass RLS.'
    """)

    op.execute("""
        CREATE FUNCTION get_agent_run_ids_for_train_snapshots() RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $$
            SELECT agent_run_id
            FROM agent_runs
            WHERE type_config->>'agent_type' IN ('critic', 'grader')
              AND type_config->'example'->>'snapshot_slug' IN (SELECT slug FROM snapshots WHERE split = 'train')
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION get_agent_run_ids_for_train_snapshots() IS
        'Returns agent_run_ids for critic/grader runs on TRAIN snapshots. SECURITY DEFINER to bypass RLS.'
    """)

    # DRY helper: can_access_snapshot - unified snapshot access check for RLS policies
    # Note: clustering branch removed (feature deprecated)
    op.execute("""
        CREATE FUNCTION can_access_snapshot(p_slug text) RETURNS boolean
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        AS $$
        BEGIN
            RETURN (
                (current_agent_type() = 'prompt_optimizer' AND is_train_snapshot(p_slug))
                OR (current_agent_type() = 'grader' AND p_slug = get_graded_snapshot_slug(current_agent_run_id()))
                OR (current_agent_type() = 'improvement' AND is_improvement_snapshot_allowed(p_slug))
            );
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION can_access_snapshot(text) IS
        'Unified snapshot access check for RLS policies. Returns TRUE if current agent can access the given snapshot.
Used by true_positives, false_positives, and their occurrence tables.'
    """)

    # DRY helper: is_own_run_as - check if run belongs to current agent with specific type
    op.execute("""
        CREATE FUNCTION is_own_run_as(p_run_id uuid, p_type text) RETURNS boolean
        LANGUAGE plpgsql STABLE
        AS $$
        BEGIN
            RETURN p_run_id = current_agent_run_id() AND current_agent_type() = p_type;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION is_own_run_as(uuid, text) IS
        'Returns TRUE if the given run_id belongs to current agent AND agent is of the specified type.
Used for critic/grader write policies.'
    """)

    # Expected recall scope functions - check if TP/FP contributes to recall denominator for a scope.
    #
    # IMPORTANT DISTINCTION:
    # - critic_scopes_expected_to_recall: Determines recall DENOMINATOR only. This is a soft expectation -
    #   critics CAN find issues outside these scopes (recall > 100% is possible!). It answers:
    #   "From which file scopes do we EXPECT a diligent critic to find this issue?"
    #
    # - graders_match_only_if_reported_on: HARD CONSTRAINT on graders. If set, graders may only
    #   give credit if the critique flagged files overlapping this set. It answers:
    #   "Where is the issue actually located / where must it be validly reported?"
    #
    # Example: file.py uses bar.py which has obvious dangerous code.
    # - critic_scopes_expected_to_recall: [[file.py], [bar.py]] - expect to find from either
    # - graders_match_only_if_reported_on: [bar.py] - issue is IN bar.py, must flag bar.py for credit
    op.execute("""
        CREATE FUNCTION is_tp_in_expected_recall_scope(
            p_snapshot_slug text,
            p_tp_id text,
            p_example_kind example_kind_enum,
            p_files_hash text
        ) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            -- Whole-snapshot scope includes all TPs in recall denominator
            SELECT CASE
                WHEN p_example_kind = 'whole_snapshot' THEN TRUE
                ELSE EXISTS (
                    -- Check if any critic_scopes_expected_to_recall entry is a subset of the reviewed scope
                    SELECT 1
                    FROM critic_scopes_expected_to_recall csetr
                    WHERE csetr.snapshot_slug = p_snapshot_slug
                      AND csetr.tp_id = p_tp_id
                      -- All files in this expected recall scope must be in the reviewed scope
                      AND NOT EXISTS (
                          SELECT 1 FROM file_set_members fsm
                          WHERE fsm.snapshot_slug = p_snapshot_slug
                            AND fsm.files_hash = csetr.files_hash
                            AND fsm.file_path NOT IN (
                                SELECT fsm2.file_path
                                FROM file_set_members fsm2
                                WHERE fsm2.snapshot_slug = p_snapshot_slug
                                  AND fsm2.files_hash = p_files_hash
                            )
                      )
                )
            END
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION is_tp_in_expected_recall_scope(text, text, example_kind_enum, text) IS
        'Returns TRUE if this TP occurrence should count toward recall denominator for the given scope.
For whole-snapshot scope, always returns TRUE.
NOTE: This determines the recall DENOMINATOR only. Critics CAN find issues outside expected scopes
(achieving >100%% recall). The graders_match_only_if_reported_on field separately constrains
where graders can give credit.'
    """)

    op.execute("""
        CREATE FUNCTION is_fp_relevant_for_scope(
            p_snapshot_slug text,
            p_fp_id text,
            p_example_kind example_kind_enum,
            p_files_hash text
        ) RETURNS boolean
        LANGUAGE sql STABLE
        AS $$
            -- Whole-snapshot scope makes all FPs relevant
            SELECT CASE
                WHEN p_example_kind = 'whole_snapshot' THEN TRUE
                ELSE EXISTS (
                    -- Check if any relevant_file for this FP is in the reviewed scope
                    SELECT 1
                    FROM false_positives fp
                    CROSS JOIN LATERAL jsonb_array_elements(fp.occurrences) AS occ
                    CROSS JOIN LATERAL jsonb_array_elements_text(occ->'relevant_files') AS rf
                    WHERE fp.snapshot_slug = p_snapshot_slug
                      AND fp.fp_id = p_fp_id
                      AND rf IN (
                          SELECT fsm.file_path
                          FROM file_set_members fsm
                          WHERE fsm.snapshot_slug = p_snapshot_slug
                            AND fsm.files_hash = p_files_hash
                      )
                )
            END
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION is_fp_relevant_for_scope(text, text, example_kind_enum, text) IS
        'Returns TRUE if any relevant_file in any FP occurrence overlaps with the reviewed scope files.
For whole-snapshot scope, always returns TRUE.'
    """)

    # Trigger functions
    op.execute("""
        CREATE FUNCTION check_edge_credit_sum() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            current_total FLOAT;
        BEGIN
            -- Get current total (excluding this row if updating)
            IF NEW.tp_id IS NOT NULL THEN
                SELECT COALESCE(SUM(credit), 0.0) INTO current_total
                FROM grading_edges
                WHERE critique_run_id = NEW.critique_run_id
                  AND tp_id = NEW.tp_id
                  AND tp_occurrence_id = NEW.tp_occurrence_id
                  AND id != COALESCE(NEW.id, -1);
            ELSE
                SELECT COALESCE(SUM(credit), 0.0) INTO current_total
                FROM grading_edges
                WHERE critique_run_id = NEW.critique_run_id
                  AND fp_id = NEW.fp_id
                  AND fp_occurrence_id = NEW.fp_occurrence_id
                  AND id != COALESCE(NEW.id, -1);
            END IF;

            -- Check if adding new credit would exceed 1.0
            IF current_total + NEW.credit > 1.0 THEN
                RAISE EXCEPTION 'Credit sum for occurrence would exceed 1.0 (current: %, new: %)',
                    current_total, NEW.credit;
            END IF;

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION check_edge_credit_sum() IS
        'Trigger function that validates credit sums per (critique_run, occurrence) do not exceed 1.0.'
    """)

    op.execute("""
        CREATE FUNCTION check_input_issue_exists() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            graded_critic_run_id UUID;
        BEGIN
            -- Get the critic run ID being graded from the grader's type_config
            graded_critic_run_id := get_graded_agent_run_id(NEW.agent_run_id);

            IF graded_critic_run_id IS NULL THEN
                RAISE EXCEPTION 'Grader run % has no graded_agent_run_id in type_config', NEW.agent_run_id;
            END IF;

            -- Check that the input_issue_id exists in reported_issues for that critic run
            IF NOT EXISTS (
                SELECT 1 FROM reported_issues
                WHERE agent_run_id = graded_critic_run_id
                  AND issue_id = NEW.input_issue_id
            ) THEN
                RAISE EXCEPTION 'Input issue % does not exist in critic run %',
                    NEW.input_issue_id, graded_critic_run_id;
            END IF;

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION check_input_issue_exists() IS
        'Validates that grading_edges.critique_issue_id exists in the graded critic run''s reported_issues'
    """)

    # Validation function (for legacy compatibility)
    op.execute("""
        CREATE FUNCTION validate_input_issue_exists(grader_run_id uuid, input_issue_id text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        AS $_$
            SELECT EXISTS (
                SELECT 1
                FROM reported_issues ri
                JOIN agent_runs gr ON gr.agent_run_id = get_graded_agent_run_id($1)
                WHERE ri.agent_run_id = gr.agent_run_id AND ri.issue_id = $2
            )
        $_$
    """)

    # Trigger function to validate grading target TP/FP exists
    op.execute("""
        CREATE FUNCTION check_grading_target_exists() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            graded_critic_run_id UUID;
            grader_snapshot_slug TEXT;
        BEGIN
            -- Get the critic run ID being graded
            graded_critic_run_id := get_graded_agent_run_id(NEW.agent_run_id);

            IF graded_critic_run_id IS NULL THEN
                RAISE EXCEPTION 'Grader run % has no graded_agent_run_id in type_config', NEW.agent_run_id;
            END IF;

            -- Get the snapshot slug from the critic run's example
            SELECT (type_config -> 'example' ->> 'snapshot_slug')
            INTO grader_snapshot_slug
            FROM agent_runs
            WHERE agent_run_id = graded_critic_run_id;

            -- Validate target TP occurrence exists
            IF NEW.target_tp_id IS NOT NULL THEN
                IF NOT EXISTS (
                    SELECT 1 FROM true_positive_occurrences
                    WHERE snapshot_slug = grader_snapshot_slug
                      AND tp_id = NEW.target_tp_id
                      AND occurrence_id = NEW.target_tp_occurrence_id
                ) THEN
                    RAISE EXCEPTION 'TP occurrence (tp_id=%, occurrence_id=%) does not exist in snapshot %',
                        NEW.target_tp_id, NEW.target_tp_occurrence_id, grader_snapshot_slug;
                END IF;
            END IF;

            -- Validate target FP occurrence exists
            IF NEW.target_fp_id IS NOT NULL THEN
                IF NOT EXISTS (
                    SELECT 1 FROM false_positive_occurrences
                    WHERE snapshot_slug = grader_snapshot_slug
                      AND fp_id = NEW.target_fp_id
                      AND occurrence_id = NEW.target_fp_occurrence_id
                ) THEN
                    RAISE EXCEPTION 'FP occurrence (fp_id=%, occurrence_id=%) does not exist in snapshot %',
                        NEW.target_fp_id, NEW.target_fp_occurrence_id, grader_snapshot_slug;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION check_grading_target_exists() IS
        'Validates that grading_edges tp_id/fp_id references exist in ground truth'
    """)

    # SECURITY DEFINER function for validation aggregates (with access guard)
    op.execute("""
        CREATE FUNCTION get_validation_full_snapshot_aggregates()
        RETURNS TABLE(
            snapshot_slug text,
            critic_image_digest text,
            critic_model text,
            grader_model text,
            critic_run_id uuid,
            grader_run_id uuid,
            status agent_run_status_enum,
            total_credit double precision,
            n_occurrences integer
        )
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path TO 'public'
        AS $$
        DECLARE
            config jsonb;
        BEGIN
            config := current_agent_type_config();

            -- Only allow whole-repo mode agents
            IF config IS NULL OR config->>'target_metric' != 'whole-repo' THEN
                RAISE EXCEPTION 'Access denied: get_validation_full_snapshot_aggregates() requires whole-repo target_metric';
            END IF;

            RETURN QUERY
            WITH occurrence_avg_credits AS (
                SELECT
                    oc.snapshot_slug,
                    oc.critic_image_digest,
                    oc.critic_model,
                    oc.grader_model,
                    oc.critic_run_id,
                    oc.grader_run_id,
                    cr.status,
                    oc.tp_id,
                    oc.occurrence_id,
                    AVG(oc.found_credit) as avg_credit
                FROM occurrence_credits oc
                JOIN snapshots s ON oc.snapshot_slug = s.slug
                JOIN agent_runs cr ON oc.critic_run_id = cr.agent_run_id
                WHERE s.split = 'valid'::split_enum
                  AND oc.example_kind = 'whole_snapshot'
                  AND (cr.type_config->>'agent_type') = 'critic'
                GROUP BY oc.snapshot_slug, oc.critic_image_digest, oc.critic_model, oc.grader_model,
                         oc.critic_run_id, oc.grader_run_id, cr.status, oc.tp_id, oc.occurrence_id
            )
            SELECT
                occurrence_avg_credits.snapshot_slug,
                occurrence_avg_credits.critic_image_digest,
                occurrence_avg_credits.critic_model,
                occurrence_avg_credits.grader_model,
                occurrence_avg_credits.critic_run_id,
                occurrence_avg_credits.grader_run_id,
                occurrence_avg_credits.status,
                SUM(avg_credit) as total_credit,
                CAST(COUNT(*) AS integer) as n_occurrences
            FROM occurrence_avg_credits
            GROUP BY occurrence_avg_credits.snapshot_slug, occurrence_avg_credits.critic_image_digest,
                     occurrence_avg_credits.critic_model, occurrence_avg_credits.grader_model,
                     occurrence_avg_credits.critic_run_id, occurrence_avg_credits.grader_run_id,
                     occurrence_avg_credits.status
            ORDER BY occurrence_avg_credits.snapshot_slug, occurrence_avg_credits.critic_image_digest,
                     occurrence_avg_credits.critic_model, occurrence_avg_credits.grader_model,
                     occurrence_avg_credits.critic_run_id, occurrence_avg_credits.grader_run_id;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION get_validation_full_snapshot_aggregates() IS
        'Black-box validation metrics for whole-repo mode.
Returns per-run recall for VALID split, whole_snapshot example_kind only.
Includes critic_run status for proper outcome counting.
Requires caller to be a whole-repo mode agent (prompt_optimizer or improvement).'
    """)

    # Line number validation trigger function for reported_issue_occurrences
    op.execute("""
        CREATE FUNCTION validate_reported_issue_line_numbers()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
          loc jsonb;
          file_path text;
          start_ln int;
          end_ln int;
          max_lines int;
          example_snapshot text;
        BEGIN
          -- Get snapshot slug from agent run
          SELECT ar.type_config -> 'example' ->> 'snapshot_slug'
          INTO example_snapshot
          FROM agent_runs ar
          JOIN reported_issues ri ON ri.agent_run_id = ar.agent_run_id
          WHERE ar.agent_run_id = NEW.agent_run_id;

          -- Iterate through locations array
          FOR loc IN SELECT * FROM jsonb_array_elements(NEW.locations)
          LOOP
            file_path := loc->>'file';
            start_ln := (loc->>'start_line')::int;
            end_ln := (loc->>'end_line')::int;

            -- Skip if no line numbers specified
            CONTINUE WHEN start_ln IS NULL AND end_ln IS NULL;

            -- Get file's line count from snapshot_files
            SELECT line_count INTO max_lines
            FROM snapshot_files sf
            WHERE sf.snapshot_slug = example_snapshot
              AND sf.relative_path = file_path;

            IF NOT FOUND THEN
              RAISE EXCEPTION 'File % not found in snapshot_files for snapshot %',
                file_path, example_snapshot;
            END IF;

            -- Validate line numbers against file bounds
            -- Use <= because line N exists in an N-line file (1-based indexing, inclusive range)
            IF start_ln IS NOT NULL AND start_ln > max_lines THEN
              RAISE EXCEPTION 'start_line % exceeds file line_count % for % (valid range: 1..%)',
                start_ln, max_lines, file_path, max_lines;
            END IF;

            IF end_ln IS NOT NULL AND end_ln > max_lines THEN
              RAISE EXCEPTION 'end_line % exceeds file line_count % for % (valid range: 1..%)',
                end_ln, max_lines, file_path, max_lines;
            END IF;
          END LOOP;

          RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION validate_reported_issue_line_numbers() IS
        'Validates reported issue line numbers against snapshot_files.line_count.
Line numbers are 1-based: for a file with line_count=N, valid range is 1..N (inclusive).
Raises exception if line numbers exceed file bounds or file not found in snapshot_files.'
    """)

    # =========================================================================
    # 5. Tables
    # =========================================================================

    # Snapshots table
    op.create_table(
        "snapshots",
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column(
            "split", postgresql.ENUM("train", "valid", "test", name="split_enum", create_type=False), nullable=False
        ),
        sa.Column("content", sa.LargeBinary(), nullable=True, comment="tar archive of source code"),
        sa.Column("source", postgresql.JSONB(), nullable=True, comment="provenance"),
        sa.Column("bundle", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("slug"),
    )

    # Agent definitions table (digest-based, OCI images)
    op.create_table(
        "agent_definitions",
        sa.Column("digest", sa.Text(), nullable=False, comment="OCI image digest (sha256:...)"),
        sa.Column(
            "agent_type",
            postgresql.ENUM(
                "critic",
                "grader",
                "prompt_optimizer",
                "clustering",
                "freeform",
                "improvement",
                name="agent_type_enum",
                create_type=False,
            ),
            nullable=False,
            comment="Agent type enum (maps to repository name in registry)",
        ),
        sa.Column(
            "created_by_agent_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Agent run that created this image (NULL for builtin images)",
        ),
        sa.Column("base_digest", sa.Text(), nullable=True, comment="Parent image digest if this is a layered image"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("digest"),
    )

    op.execute(
        "COMMENT ON TABLE agent_definitions IS 'Agent images as OCI digests. Registry proxy writes rows on manifest push.'"
    )

    # Agent role salt table (singleton)
    op.create_table(
        "agent_role_salt",
        sa.Column("id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("salt", sa.LargeBinary(), server_default=sa.text("gen_random_bytes(32)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="agent_role_salt_id_check"),
    )

    op.execute(
        "COMMENT ON TABLE agent_role_salt IS 'Singleton containing salt for deterministic agent password derivation'"
    )
    op.execute("REVOKE ALL ON agent_role_salt FROM PUBLIC")

    # Agent runs table
    op.create_table(
        "agent_runs",
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "image_digest", sa.Text(), nullable=False, comment="OCI image digest (FK to agent_definitions.digest)"
        ),
        sa.Column("parent_agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("type_config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "in_progress",
                "completed",
                "max_turns_exceeded",
                "context_length_exceeded",
                "reported_failure",
                name="agent_run_status_enum",
                create_type=False,
            ),
            server_default=sa.text("'in_progress'"),
            nullable=False,
        ),
        sa.Column("completion_summary", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("agent_run_id"),
        sa.ForeignKeyConstraint(["image_digest"], ["agent_definitions.digest"]),
        sa.ForeignKeyConstraint(["parent_agent_run_id"], ["agent_runs.agent_run_id"]),
    )

    op.execute(
        "COMMENT ON TABLE agent_runs IS 'Unified table for all agent runs (critics, graders, optimizers, freeform)'"
    )
    op.execute(
        "COMMENT ON COLUMN agent_runs.parent_agent_run_id IS 'Parent agent that spawned this sub-agent (NULL for top-level)'"
    )
    op.execute(
        "COMMENT ON COLUMN agent_runs.type_config IS 'JSONB with agent_type discriminator and type-specific fields'"
    )
    op.execute(
        "COMMENT ON COLUMN agent_runs.status IS 'Run status: in_progress, completed, max_turns_exceeded, context_length_exceeded, or reported_failure'"
    )
    op.execute(
        "COMMENT ON COLUMN agent_runs.completion_summary IS 'Markdown summary from agent when status=completed, or error message when status=reported_failure'"
    )

    # Add FK from agent_definitions to agent_runs (circular reference)
    op.create_foreign_key(
        "fk_agent_definitions_created_by",
        "agent_definitions",
        "agent_runs",
        ["created_by_agent_run_id"],
        ["agent_run_id"],
    )

    op.execute("COMMENT ON COLUMN agent_runs.image_digest IS 'OCI image digest (FK to agent_definitions.digest)'")

    # Snapshot files table - all files in each snapshot for FK validation
    op.create_table(
        "snapshot_files",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("relative_path", sa.String(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "relative_path"),
        sa.ForeignKeyConstraint(["snapshot_slug"], ["snapshots.slug"], ondelete="RESTRICT"),
    )

    op.execute(
        "COMMENT ON TABLE snapshot_files IS 'All files in each snapshot. Used for FK validation of file paths in occurrences and trigger sets.'"
    )
    op.execute(
        "COMMENT ON COLUMN snapshot_files.relative_path IS "
        "'Path relative to snapshot root (e.g., \"src/utils.py\"). NOT absolute paths.'"
    )

    # True positives table (issue header - occurrences are in separate table)
    op.create_table(
        "true_positives",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("tp_id", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "tp_id"),
        sa.ForeignKeyConstraint(["snapshot_slug"], ["snapshots.slug"], ondelete="CASCADE"),
        issue_id_constraint("tp_id", "tp_id_format"),
    )

    # False positives table (issue header - occurrences are in separate table)
    op.create_table(
        "false_positives",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("fp_id", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "fp_id"),
        sa.ForeignKeyConstraint(["snapshot_slug"], ["snapshots.slug"], ondelete="CASCADE"),
        issue_id_constraint("fp_id", "fp_id_format"),
    )

    op.execute(
        "COMMENT ON TABLE false_positives IS "
        "'Patterns the labeler considers acceptable - teaches agents what NOT to flag.'"
    )

    # File sets table - deduplicated, content-addressable by (snapshot_slug, files_hash)
    op.create_table(
        "file_sets",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("files_hash", sa.String(), nullable=False),  # MD5 of sorted file paths
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "files_hash"),
        sa.ForeignKeyConstraint(["snapshot_slug"], ["snapshots.slug"], ondelete="RESTRICT"),
    )

    op.execute("""
        COMMENT ON TABLE file_sets IS
        'Content-addressable file sets for training examples.
Primary key is (snapshot_slug, files_hash) where files_hash = MD5 of sorted file paths.
Deduplicated by PK constraint - same files always produce same hash.'
    """)

    # File set members table - files in each file set (FK-validated)
    op.create_table(
        "file_set_members",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("files_hash", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "files_hash", "file_path"),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "files_hash"], ["file_sets.snapshot_slug", "file_sets.files_hash"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "file_path"],
            ["snapshot_files.snapshot_slug", "snapshot_files.relative_path"],
            ondelete="CASCADE",
        ),
    )

    op.execute("""
        COMMENT ON TABLE file_set_members IS
        'Files belonging to each file set. FK to snapshot_files validates file paths exist in snapshot.'
    """)

    # True positive occurrences table
    # Note: critic_scopes_expected_to_recall is stored in critic_scopes_expected_to_recall M:N table, not as JSONB column
    # Note: files/line ranges are stored in tp_occurrence_ranges table, not as JSONB column
    op.create_table(
        "true_positive_occurrences",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("tp_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("graders_match_only_if_reported_on", sa.Text(), nullable=True),  # FK to file_sets
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "tp_id", "occurrence_id"),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "tp_id"], ["true_positives.snapshot_slug", "true_positives.tp_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "graders_match_only_if_reported_on"],
            ["file_sets.snapshot_slug", "file_sets.files_hash"],
            ondelete="RESTRICT",
            name="fk_tp_occ_matchable_files",
        ),
    )

    # False positive occurrences table
    # Note: files/line ranges are stored in fp_occurrence_ranges table, not as JSONB column
    # Note: relevant_files are stored in fp_occurrence_relevant_files table, not as JSONB column
    op.create_table(
        "false_positive_occurrences",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("fp_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("graders_match_only_if_reported_on", sa.Text(), nullable=True),  # FK to file_sets
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "fp_id", "occurrence_id"),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "fp_id"], ["false_positives.snapshot_slug", "false_positives.fp_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "graders_match_only_if_reported_on"],
            ["file_sets.snapshot_slug", "file_sets.files_hash"],
            ondelete="RESTRICT",
            name="fk_fp_occ_matchable_files",
        ),
    )

    # =========================================================================
    # Normalized occurrence range tables
    # =========================================================================
    # These tables normalize the JSONB files column into proper relational tables
    # for better queryability, foreign key validation, and line number validation.
    # Uses exclusive arc pattern: exactly one of tp_id or fp_id must be set.

    # Occurrence ranges table (shared by TPs and FPs)
    op.create_table(
        "occurrence_ranges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("tp_id", sa.String(), nullable=True),
        sa.Column("fp_id", sa.String(), nullable=True),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("range_id", sa.Integer(), nullable=False, comment="0-based index within file"),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_slug", "tp_id", "fp_id", "occurrence_id", "file_path", "range_id", name="uq_occurrence_ranges"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "tp_id", "occurrence_id"],
            [
                "true_positive_occurrences.snapshot_slug",
                "true_positive_occurrences.tp_id",
                "true_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
            name="fk_occurrence_range_tp",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "fp_id", "occurrence_id"],
            [
                "false_positive_occurrences.snapshot_slug",
                "false_positive_occurrences.fp_id",
                "false_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
            name="fk_occurrence_range_fp",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "file_path"],
            ["snapshot_files.snapshot_slug", "snapshot_files.relative_path"],
            ondelete="CASCADE",
            name="fk_occurrence_range_snapshot_file",
        ),
        sa.CheckConstraint("start_line >= 1", name="occurrence_range_start_line_positive"),
        sa.CheckConstraint("end_line >= start_line", name="occurrence_range_end_gte_start"),
        sa.CheckConstraint("(tp_id IS NULL) <> (fp_id IS NULL)", name="occurrence_range_exclusive_arc"),
    )

    op.execute(
        "COMMENT ON TABLE occurrence_ranges IS "
        "'Line ranges within TP/FP occurrences (normalized from files JSONB). "
        "Exactly one of tp_id or fp_id must be set (exclusive arc pattern).'"
    )

    # False positive relevant files table
    op.create_table(
        "fp_occurrence_relevant_files",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("fp_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "fp_id", "occurrence_id", "file_path"),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "fp_id", "occurrence_id"],
            [
                "false_positive_occurrences.snapshot_slug",
                "false_positive_occurrences.fp_id",
                "false_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "file_path"],
            ["snapshot_files.snapshot_slug", "snapshot_files.relative_path"],
            ondelete="CASCADE",
            name="fk_fp_relevant_file_snapshot_file",
        ),
    )

    op.execute(
        "COMMENT ON TABLE fp_occurrence_relevant_files IS 'Files that make false positive occurrences relevant (normalized from relevant_files JSONB)'"
    )

    # Trigger to validate line ranges don't exceed file line counts
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_range_line_numbers()
        RETURNS TRIGGER AS $$
        DECLARE
            file_line_count INT;
        BEGIN
            -- Get line count for the referenced file
            SELECT line_count INTO file_line_count
            FROM snapshot_files
            WHERE snapshot_slug = NEW.snapshot_slug
              AND relative_path = NEW.file_path;

            -- Validate end_line is within bounds
            IF NEW.end_line > file_line_count THEN
                RAISE EXCEPTION 'Line range [%, %] exceeds file line count % for file % in snapshot %',
                    NEW.start_line, NEW.end_line, file_line_count, NEW.file_path, NEW.snapshot_slug;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        COMMENT ON FUNCTION validate_range_line_numbers() IS
        'Validates that line ranges do not exceed the actual file line count from snapshot_files. '
        'This ensures ground truth references only valid line numbers within files.';
    """)

    op.execute("""
        CREATE TRIGGER validate_occurrence_range_bounds
        BEFORE INSERT OR UPDATE ON occurrence_ranges
        FOR EACH ROW
        EXECUTE FUNCTION validate_range_line_numbers();
    """)

    # Basic line number validation for reported_issue_occurrences
    # (different structure: locations array with {file, start_line, end_line} objects)
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_reported_issue_occ_basic_line_numbers()
        RETURNS TRIGGER AS $$
        DECLARE
            invalid_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO invalid_count
            FROM jsonb_array_elements(NEW.locations) AS loc
            WHERE (loc->>'start_line' IS NOT NULL AND (loc->>'start_line')::int < 1)
               OR (loc->>'end_line' IS NOT NULL AND (loc->>'end_line')::int < 1)
               OR (loc->>'start_line' IS NOT NULL AND loc->>'end_line' IS NOT NULL
                   AND (loc->>'end_line')::int < (loc->>'start_line')::int);

            IF invalid_count > 0 THEN
                RAISE EXCEPTION 'Invalid line numbers in reported issue occurrence: line numbers must be >= 1 and end_line >= start_line';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        COMMENT ON FUNCTION validate_reported_issue_occ_basic_line_numbers() IS
        'Validates basic line number constraints for reported_issue_occurrences: positive (>= 1) and end_line >= start_line.
        Cross-table validation against snapshot_files.line_count is done by validate_reported_issue_line_numbers().';
    """)

    # Occurrence triggers - M:N linking occurrences to file sets
    op.create_table(
        "critic_scopes_expected_to_recall",
        sa.Column("snapshot_slug", sa.String(), nullable=False),
        sa.Column("tp_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("files_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_slug", "tp_id", "occurrence_id", "files_hash"),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "tp_id", "occurrence_id"],
            [
                "true_positive_occurrences.snapshot_slug",
                "true_positive_occurrences.tp_id",
                "true_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "files_hash"], ["file_sets.snapshot_slug", "file_sets.files_hash"], ondelete="CASCADE"
        ),
    )

    op.execute("""
        COMMENT ON TABLE critic_scopes_expected_to_recall IS
        'M:N linking TP occurrences to file_sets defining EXPECTED recall scopes.

DETERMINES: Recall DENOMINATOR only. "From which scopes do we expect critics to find this issue?"
Each occurrence may have multiple alternative scopes (OR logic: any one suffices).

DOES NOT CONSTRAIN: Critics CAN find issues outside expected scopes (recall >100%% possible).
A diligent critic reviewing file.py might discover issues in bar.py it depends on.

DISTINCT FROM graders_match_only_if_reported_on: That field is a HARD constraint on where
graders can give credit. This field only affects metric denominators.'
    """)

    # Reported issues table
    op.create_table(
        "reported_issues",
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_id", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("agent_run_id", "issue_id"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.agent_run_id"], ondelete="CASCADE"),
        issue_id_constraint("issue_id", "issue_id_format"),
    )

    op.execute(
        "COMMENT ON COLUMN reported_issues.agent_run_id IS 'FK to agent_runs - identifies which agent run reported this issue'"
    )

    op.execute("""
        COMMENT ON TABLE reported_issues IS
        'Issues reported by critic agents. Each issue has a rationale and one or more occurrences.
Linked to agent_runs via agent_run_id. Occurrences in reported_issue_occurrences.

RLS: Critic sees own run only. Grader sees graded run only. Prompt optimizer sees TRAIN runs.

USEFUL FOR: Critic (write), grader (read), clustering (read).
- Critic: INSERT new findings during review
- Grader: read to match against ground truth
- Clustering: read unknowns (issues with no TP/FP match)'
    """)

    # Reported issue occurrences table
    op.create_table(
        "reported_issue_occurrences",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reported_issue_id", sa.String(), nullable=False),
        sa.Column(
            "locations",
            postgresql.JSONB(),
            nullable=False,
            comment="1+ location anchors: {file, start_line?, end_line?}",
        ),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_run_id", "reported_issue_id"],
            ["reported_issues.agent_run_id", "reported_issues.issue_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("jsonb_array_length(locations) > 0", name="locations_not_empty"),
    )

    op.execute(
        "COMMENT ON COLUMN reported_issue_occurrences.agent_run_id IS 'FK to agent_runs (denormalized from reported_issues for RLS efficiency)'"
    )

    op.execute("""
        COMMENT ON TABLE reported_issue_occurrences IS
        'Locations where a reported issue occurs. Each occurrence has file path and line range.
Foreign key to reported_issues(agent_run_id, issue_id).

RLS: Same as reported_issues (scoped by agent_run_id).

USEFUL FOR: Critic (write), grader (read).
- Critic: INSERT occurrence locations when reporting issues
- Grader: read to verify location matches ground truth'
    """)

    op.execute("""
        CREATE TRIGGER validate_reported_issue_occ_basic_line_numbers_trigger
        BEFORE INSERT OR UPDATE ON reported_issue_occurrences
        FOR EACH ROW EXECUTE FUNCTION validate_reported_issue_occ_basic_line_numbers();
    """)

    # Grading edges table - bipartite graph between critique issues and GT occurrences
    op.create_table(
        "grading_edges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # Reference to the critique issue (from reported_issues)
        sa.Column("critique_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("critique_issue_id", sa.String(), nullable=False),
        # TP target (nullable - exactly one of TP or FP must be set)
        sa.Column("snapshot_slug", sa.String(), nullable=False),  # For FK validation
        sa.Column("tp_id", sa.String(), nullable=True),
        sa.Column("tp_occurrence_id", sa.String(), nullable=True),
        # FP target (nullable)
        sa.Column("fp_id", sa.String(), nullable=True),
        sa.Column("fp_occurrence_id", sa.String(), nullable=True),
        # Grading metadata
        sa.Column("credit", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("grader_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # Primary key
        sa.PrimaryKeyConstraint("id"),
        # FK to reported_issues
        sa.ForeignKeyConstraint(
            ["critique_run_id", "critique_issue_id"],
            ["reported_issues.agent_run_id", "reported_issues.issue_id"],
            ondelete="CASCADE",
            name="fk_grading_edges_critique",
        ),
        # FK to agent_runs (grader)
        sa.ForeignKeyConstraint(
            ["grader_run_id"], ["agent_runs.agent_run_id"], ondelete="CASCADE", name="fk_grading_edges_grader"
        ),
        # FK to true_positive_occurrences (when TP target set)
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "tp_id", "tp_occurrence_id"],
            [
                "true_positive_occurrences.snapshot_slug",
                "true_positive_occurrences.tp_id",
                "true_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
            name="fk_grading_edges_tp",
        ),
        # FK to false_positive_occurrences (when FP target set)
        sa.ForeignKeyConstraint(
            ["snapshot_slug", "fp_id", "fp_occurrence_id"],
            [
                "false_positive_occurrences.snapshot_slug",
                "false_positive_occurrences.fp_id",
                "false_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
            name="fk_grading_edges_fp",
        ),
        # Unique constraints to prevent duplicate edges
        sa.UniqueConstraint(
            "critique_run_id", "critique_issue_id", "tp_id", "tp_occurrence_id", name="uq_grading_edges_tp"
        ),
        sa.UniqueConstraint(
            "critique_run_id", "critique_issue_id", "fp_id", "fp_occurrence_id", name="uq_grading_edges_fp"
        ),
        # Exactly one of TP or FP must be set (same pattern as old grading_decisions)
        sa.CheckConstraint(
            """(
                (tp_id IS NOT NULL AND tp_occurrence_id IS NOT NULL AND fp_id IS NULL AND fp_occurrence_id IS NULL)
                OR (fp_id IS NOT NULL AND fp_occurrence_id IS NOT NULL AND tp_id IS NULL AND tp_occurrence_id IS NULL)
            )""",
            name="exactly_one_target_edge",
        ),
        # Credit range
        sa.CheckConstraint("credit >= 0.0 AND credit <= 1.0", name="credit_range_edge"),
    )

    op.execute("""
        COMMENT ON TABLE grading_edges IS
        'Explicit bipartite graph edges from critique issues to GT occurrences.
Each edge represents a grader''s judgment about whether a critique issue matches a GT occurrence.

Key invariants:
- Every (critique_issue, matchable_occurrence) pair must have an edge (complete coverage)
- Exactly one of (tp_id, tp_occurrence_id) or (fp_id, fp_occurrence_id) is set
- credit: 0.0-1.0 for TP matches, 0.0 for FP matches (FP credit = anti-credit, penalty)
- No-match decisions still create edges with credit=0.0

Drift = missing edges. Query grading_pending view to see what''s missing.

USEFUL FOR: Grader (write), prompt optimizer (read TRAIN only).
- Grader: INSERT edges for each (critique_issue, gt_occurrence) pair
- Prompt optimizer: analyze which issues got credit vs not
- Clustering: read decisions with NULL targets (unknowns)'
    """)

    # Events table
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_num", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.agent_run_id"], ondelete="CASCADE", name="fk_events_agent_run_id"
        ),
        sa.UniqueConstraint("agent_run_id", "sequence_num", name="uq_events_agent_run_id_seq"),
    )

    # Model metadata table
    op.create_table(
        "model_metadata",
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("input_usd_per_1m_tokens", sa.Float(), nullable=False),
        sa.Column("cached_input_usd_per_1m_tokens", sa.Float(), nullable=True),
        sa.Column("output_usd_per_1m_tokens", sa.Float(), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("model_id"),
    )

    # =========================================================================
    # 6. Examples VIEW (auto-generated from snapshots + file_sets)
    # =========================================================================
    op.execute("""
        CREATE VIEW examples AS
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
                  AND is_tp_in_expected_recall_scope(fs.snapshot_slug, t.tp_id, 'file_set'::example_kind_enum, fs.files_hash)
            ), 0)::integer AS recall_denominator
        FROM file_sets fs
    """)

    op.execute("""
        COMMENT ON VIEW examples IS
        'Training/evaluation examples. Each defines a scope (whole-snapshot or file-set).

recall_denominator = count of TP occurrences in expected recall scope for this example.
Computed via is_tp_in_expected_recall_scope() which checks critic_scopes_expected_to_recall.

IMPORTANT: recall_denominator is the EXPECTED count, not a hard limit.
Critics CAN find issues outside expected scopes, achieving >100%% recall.
A diligent critic reviewing file.py might discover issues in bar.py it depends on.'
    """)

    # ============================================================================
    # 2. Helper functions for snapshot_grader agent type
    # ============================================================================
    op.execute("""
        CREATE FUNCTION current_grader_snapshot_slug() RETURNS TEXT
        LANGUAGE SQL STABLE SECURITY DEFINER AS $$
            SELECT (type_config->>'snapshot_slug')::text
            FROM agent_runs
            WHERE agent_run_id = current_agent_run_id()
              AND (type_config->>'agent_type') = 'snapshot_grader'
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION current_grader_snapshot_slug() IS
        'Returns snapshot_slug for snapshot_grader agents. NULL for other agent types.
Used by RLS to scope snapshot-wide access for grader daemons.'
    """)

    op.execute("""
        CREATE FUNCTION is_critique_on_grader_snapshot(p_critique_run_id UUID) RETURNS BOOLEAN
        LANGUAGE SQL STABLE SECURITY DEFINER AS $$
            SELECT EXISTS (
                SELECT 1 FROM agent_runs critique
                WHERE critique.agent_run_id = p_critique_run_id
                  AND (critique.type_config->>'agent_type') = 'critic'
                  AND (critique.type_config->'example'->>'snapshot_slug') = current_grader_snapshot_slug()
            )
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION is_critique_on_grader_snapshot(UUID) IS
        'Returns TRUE if the given critique run is on the current snapshot_grader daemon''s snapshot.
Used by RLS to allow daemon access to all critiques for its snapshot.'
    """)

    # ============================================================================
    # 3. matchable_occurrences() function for sparse graph matching
    # ============================================================================
    op.execute("""
        CREATE FUNCTION matchable_occurrences(
            p_snapshot_slug VARCHAR,
            p_files VARCHAR[]
        ) RETURNS TABLE (
            tp_id VARCHAR,
            tp_occurrence_id VARCHAR,
            fp_id VARCHAR,
            fp_occurrence_id VARCHAR
        ) AS $$
            -- TPs: cross-cutting (NULL) or file overlap
            SELECT tpo.tp_id, tpo.occurrence_id, NULL::VARCHAR, NULL::VARCHAR
            FROM true_positive_occurrences tpo
            WHERE tpo.snapshot_slug = p_snapshot_slug
              AND (
                  tpo.graders_match_only_if_reported_on IS NULL
                  OR EXISTS (
                      SELECT 1 FROM file_set_members fsm
                      WHERE fsm.snapshot_slug = tpo.snapshot_slug
                        AND fsm.files_hash = tpo.graders_match_only_if_reported_on
                        AND fsm.file_path = ANY(p_files)
                  )
              )
            UNION ALL
            -- FPs: cross-cutting (NULL) or file overlap
            SELECT NULL, NULL, fpo.fp_id, fpo.occurrence_id
            FROM false_positive_occurrences fpo
            WHERE fpo.snapshot_slug = p_snapshot_slug
              AND (
                  fpo.graders_match_only_if_reported_on IS NULL
                  OR EXISTS (
                      SELECT 1 FROM file_set_members fsm
                      WHERE fsm.snapshot_slug = fpo.snapshot_slug
                        AND fsm.files_hash = fpo.graders_match_only_if_reported_on
                        AND fsm.file_path = ANY(p_files)
                  )
              )
        $$ LANGUAGE SQL STABLE
    """)

    op.execute("""
        COMMENT ON FUNCTION matchable_occurrences(VARCHAR, VARCHAR[]) IS
        'Returns GT occurrences matchable from given files for a snapshot.
Used by:
- grading_pending view (drift detection)
- Edge validation trigger
- Workload estimation

NULL graders_match_only_if_reported_on = cross-cutting (any critique can match)
Non-NULL = file-local (only critiques touching those files can match)'
    """)

    # Index for efficient file-local lookups
    op.create_index("idx_file_set_members_file_path", "file_set_members", ["snapshot_slug", "file_path"])

    # ============================================================================
    # 4. grading_pending view for drift detection
    # ============================================================================
    op.execute("""
        CREATE VIEW grading_pending AS
        WITH critique_issues AS (
            -- Get all critique issues with their files and snapshot
            SELECT
                ri.agent_run_id AS critique_run_id,
                ri.issue_id AS critique_issue_id,
                (ar.type_config->'example'->>'snapshot_slug') AS snapshot_slug,
                array_agg(DISTINCT loc->>'file') FILTER (WHERE loc->>'file' IS NOT NULL) AS reported_files
            FROM reported_issues ri
            JOIN agent_runs ar ON ar.agent_run_id = ri.agent_run_id
            LEFT JOIN reported_issue_occurrences rio ON rio.agent_run_id = ri.agent_run_id AND rio.reported_issue_id = ri.issue_id
            LEFT JOIN LATERAL jsonb_array_elements(rio.locations) AS loc ON true
            WHERE (ar.type_config->>'agent_type') = 'critic'
              AND ar.status = 'completed'
            GROUP BY ri.agent_run_id, ri.issue_id, ar.type_config
        )
        -- Find missing TP edges
        SELECT
            ci.critique_run_id,
            ci.critique_issue_id,
            ci.snapshot_slug,
            mo.tp_id,
            mo.tp_occurrence_id,
            NULL::VARCHAR AS fp_id,
            NULL::VARCHAR AS fp_occurrence_id
        FROM critique_issues ci
        CROSS JOIN LATERAL matchable_occurrences(ci.snapshot_slug, ci.reported_files) mo
        WHERE mo.tp_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM grading_edges ge
              WHERE ge.critique_run_id = ci.critique_run_id
                AND ge.critique_issue_id = ci.critique_issue_id
                AND ge.tp_id = mo.tp_id
                AND ge.tp_occurrence_id = mo.tp_occurrence_id
          )
        UNION ALL
        -- Find missing FP edges
        SELECT
            ci.critique_run_id,
            ci.critique_issue_id,
            ci.snapshot_slug,
            NULL::VARCHAR AS tp_id,
            NULL::VARCHAR AS tp_occurrence_id,
            mo.fp_id,
            mo.fp_occurrence_id
        FROM critique_issues ci
        CROSS JOIN LATERAL matchable_occurrences(ci.snapshot_slug, ci.reported_files) mo
        WHERE mo.fp_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM grading_edges ge
              WHERE ge.critique_run_id = ci.critique_run_id
                AND ge.critique_issue_id = ci.critique_issue_id
                AND ge.fp_id = mo.fp_id
                AND ge.fp_occurrence_id = mo.fp_occurrence_id
          )
    """)

    op.execute("""
        COMMENT ON VIEW grading_pending IS
        'Shows missing grading edges (drift).
Each row represents a (critique_issue, gt_occurrence) pair that needs grading.

Query patterns:
- All drift: SELECT * FROM grading_pending
- By snapshot: WHERE snapshot_slug = ''...''
- By critique: WHERE critique_run_id = ''...''
- By GT: WHERE tp_id = ''...'' AND tp_occurrence_id = ''...''

When this view returns no rows for a grader''s scope, grading is complete.'
    """)

    # ============================================================================
    # 5. RLS policies for grading_edges
    # ============================================================================
    op.execute("ALTER TABLE grading_edges ENABLE ROW LEVEL SECURITY")

    # Per-critique grader (existing pattern) - writes edges for graded critique only
    op.execute("""
        CREATE POLICY grader_insert_edges ON grading_edges FOR INSERT WITH CHECK (
            current_agent_type() = 'grader'
            AND grader_run_id = current_agent_run_id()
            AND critique_run_id = current_graded_agent_run_id()
        )
    """)

    op.execute("""
        CREATE POLICY grader_select_edges ON grading_edges FOR SELECT USING (
            current_agent_type() = 'grader'
            AND (
                grader_run_id = current_agent_run_id()
                OR critique_run_id = current_graded_agent_run_id()
            )
        )
    """)

    op.execute("""
        CREATE POLICY grader_update_edges ON grading_edges FOR UPDATE USING (
            current_agent_type() = 'grader'
            AND grader_run_id = current_agent_run_id()
        )
    """)

    op.execute("""
        CREATE POLICY grader_delete_edges ON grading_edges FOR DELETE USING (
            current_agent_type() = 'grader'
            AND grader_run_id = current_agent_run_id()
        )
    """)

    # Snapshot grader (daemon) - writes edges for any critique on its snapshot
    op.execute("""
        CREATE POLICY snapshot_grader_insert_edges ON grading_edges FOR INSERT WITH CHECK (
            current_agent_type() = 'snapshot_grader'
            AND grader_run_id = current_agent_run_id()
            AND is_critique_on_grader_snapshot(critique_run_id)
        )
    """)

    op.execute("""
        CREATE POLICY snapshot_grader_select_edges ON grading_edges FOR SELECT USING (
            current_agent_type() = 'snapshot_grader'
            AND is_critique_on_grader_snapshot(critique_run_id)
        )
    """)

    op.execute("""
        CREATE POLICY snapshot_grader_update_edges ON grading_edges FOR UPDATE USING (
            current_agent_type() = 'snapshot_grader'
            AND grader_run_id = current_agent_run_id()
        )
    """)

    op.execute("""
        CREATE POLICY snapshot_grader_delete_edges ON grading_edges FOR DELETE USING (
            current_agent_type() = 'snapshot_grader'
            AND grader_run_id = current_agent_run_id()
        )
    """)

    # Prompt optimizer - read TRAIN edges
    op.execute("""
        CREATE POLICY prompt_optimizer_select_edges ON grading_edges FOR SELECT USING (
            current_agent_type() = 'prompt_optimizer'
            AND is_train_snapshot(snapshot_slug)
        )
    """)

    # Improvement agent - read allowed edges
    op.execute("""
        CREATE POLICY improvement_select_edges ON grading_edges FOR SELECT USING (
            current_agent_type() = 'improvement'
            AND is_improvement_snapshot_allowed(snapshot_slug)
        )
    """)

    # Admin full access
    op.execute("""
        CREATE POLICY admin_full_access_edges ON grading_edges FOR ALL USING (
            current_user = 'postgres'
        )
    """)

    # ============================================================================
    # 6. RLS policies for snapshot_grader on reported_issues
    # ============================================================================
    op.execute("""
        CREATE POLICY snapshot_grader_read_critiques ON reported_issues FOR SELECT USING (
            current_agent_type() = 'snapshot_grader'
            AND is_critique_on_grader_snapshot(agent_run_id)
        )
    """)

    op.execute("""
        CREATE POLICY snapshot_grader_read_critique_occs ON reported_issue_occurrences FOR SELECT USING (
            current_agent_type() = 'snapshot_grader'
            AND is_critique_on_grader_snapshot(agent_run_id)
        )
    """)

    # ============================================================================
    # 7. RLS policy for snapshot_grader on agent_runs
    # ============================================================================
    op.execute("""
        CREATE POLICY snapshot_grader_read_runs ON agent_runs FOR SELECT USING (
            current_agent_type() = 'snapshot_grader'
            AND (
                agent_run_id = current_agent_run_id()
                OR (
                    (type_config->>'agent_type') = 'critic'
                    AND (type_config->'example'->>'snapshot_slug') = current_grader_snapshot_slug()
                )
            )
        )
    """)

    # ============================================================================
    # 8. Update can_access_snapshot() for snapshot_grader
    # ============================================================================
    # Drop and recreate the function to add snapshot_grader support
    op.execute("DROP FUNCTION IF EXISTS can_access_snapshot(VARCHAR)")

    op.execute("""
        CREATE FUNCTION can_access_snapshot(p_slug VARCHAR) RETURNS BOOLEAN
        LANGUAGE plpgsql STABLE SECURITY DEFINER AS $$
        BEGIN
            RETURN (
                (current_agent_type() = 'prompt_optimizer' AND is_train_snapshot(p_slug))
                OR (current_agent_type() = 'grader' AND p_slug = get_graded_snapshot_slug(current_agent_run_id()))
                OR (current_agent_type() = 'snapshot_grader' AND p_slug = current_grader_snapshot_slug())
                OR (current_agent_type() = 'improvement' AND is_improvement_snapshot_allowed(p_slug))
            );
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION can_access_snapshot(VARCHAR) IS
        'Checks if current agent can access a snapshot''s ground truth.
- prompt_optimizer: TRAIN snapshots only
- grader: snapshot of the critique being graded
- snapshot_grader: the daemon''s assigned snapshot
- improvement: allowed snapshots from config'
    """)

    # ============================================================================
    # 9. pg_notify triggers for GT changes (daemon wake-up)
    # ============================================================================
    op.execute("""
        CREATE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('grading_pending', json_build_object(
                'event', TG_OP || '_' || TG_TABLE_NAME,
                'snapshot_slug', COALESCE(NEW.snapshot_slug, OLD.snapshot_slug)
            )::text);
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        COMMENT ON FUNCTION notify_gt_changed() IS
        'Sends pg_notify when ground truth changes. Used to wake snapshot_grader daemons.
Fires on INSERT/DELETE of TPs/FPs (not UPDATE - minor wording fixes don''t need re-grade).'
    """)

    # Triggers on TP/FP tables (INSERT/DELETE only)
    op.execute("""
        CREATE TRIGGER trg_notify_tp_changed
        AFTER INSERT OR DELETE ON true_positives
        FOR EACH ROW EXECUTE FUNCTION notify_gt_changed()
    """)

    op.execute("""
        CREATE TRIGGER trg_notify_tp_occ_changed
        AFTER INSERT OR DELETE ON true_positive_occurrences
        FOR EACH ROW EXECUTE FUNCTION notify_gt_changed()
    """)

    op.execute("""
        CREATE TRIGGER trg_notify_fp_changed
        AFTER INSERT OR DELETE ON false_positives
        FOR EACH ROW EXECUTE FUNCTION notify_gt_changed()
    """)

    op.execute("""
        CREATE TRIGGER trg_notify_fp_occ_changed
        AFTER INSERT OR DELETE ON false_positive_occurrences
        FOR EACH ROW EXECUTE FUNCTION notify_gt_changed()
    """)

    # ============================================================================
    # 10. Credit sum enforcement for grading_edges
    # ============================================================================
    # View to aggregate credit sums per (critique_run, gt_occurrence)
    op.execute("""
        CREATE VIEW grading_edge_credit_sums AS
        SELECT
            critique_run_id,
            tp_id, tp_occurrence_id,
            fp_id, fp_occurrence_id,
            SUM(credit) AS total_credit
        FROM grading_edges
        GROUP BY critique_run_id, tp_id, tp_occurrence_id, fp_id, fp_occurrence_id
    """)

    op.execute("""
        COMMENT ON VIEW grading_edge_credit_sums IS
        'Aggregate credit sums per (critique_run, occurrence) for enforcing credit <= 1.0 constraint.
Used by check_edge_credit_sum trigger function.'
    """)

    # Create trigger using the function defined earlier
    op.execute("""
        CREATE TRIGGER enforce_edge_credit_sum
        BEFORE INSERT OR UPDATE ON grading_edges
        FOR EACH ROW EXECUTE FUNCTION check_edge_credit_sum()
    """)

    # =========================================================================
    # 11. Match filter scope enforcement for grading_edges
    # =========================================================================
    # Ensures that if a TP/FP occurrence has graders_match_only_if_reported_on set,
    # edges to it can only come from critique issues reported on files in that set.
    op.execute("""
        CREATE FUNCTION check_edge_matches_filter_scope() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            filter_hash TEXT;
        BEGIN
            -- Get the graders_match_only_if_reported_on for the target occurrence
            IF NEW.tp_id IS NOT NULL THEN
                SELECT graders_match_only_if_reported_on INTO filter_hash
                FROM true_positive_occurrences
                WHERE snapshot_slug = NEW.snapshot_slug
                  AND tp_id = NEW.tp_id
                  AND occurrence_id = NEW.tp_occurrence_id;
            ELSE
                SELECT graders_match_only_if_reported_on INTO filter_hash
                FROM false_positive_occurrences
                WHERE snapshot_slug = NEW.snapshot_slug
                  AND fp_id = NEW.fp_id
                  AND occurrence_id = NEW.fp_occurrence_id;
            END IF;

            -- If no filter, allow
            IF filter_hash IS NULL THEN
                RETURN NEW;
            END IF;

            -- Check all files from the critique issue are in the filter's file set
            -- reported_issue_occurrences.locations is JSONB array: [{file, start_line?, end_line?}, ...]
            IF EXISTS (
                SELECT 1 FROM reported_issue_occurrences rio
                CROSS JOIN LATERAL jsonb_array_elements(rio.locations) AS loc
                WHERE rio.agent_run_id = NEW.critique_run_id
                  AND rio.reported_issue_id = NEW.critique_issue_id
                  AND loc->>'file' NOT IN (
                      SELECT file_path FROM file_set_members
                      WHERE snapshot_slug = NEW.snapshot_slug
                        AND files_hash = filter_hash
                  )
            ) THEN
                RAISE EXCEPTION 'Critique issue % reports files outside target occurrence graders_match_only_if_reported_on scope (filter: %)',
                    NEW.critique_issue_id, filter_hash;
            END IF;

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        COMMENT ON FUNCTION check_edge_matches_filter_scope() IS
        'Validates that grading edges only target occurrences whose graders_match_only_if_reported_on
includes the files where the critique issue was reported. Prevents matching
a critique to an occurrence that could not have been found from those files.'
    """)

    op.execute("""
        CREATE TRIGGER enforce_edge_filter_scope
        BEFORE INSERT OR UPDATE ON grading_edges
        FOR EACH ROW EXECUTE FUNCTION check_edge_matches_filter_scope()
    """)

    # ============================================================================
    # 12. Recreate recall views using grading_edges
    # ============================================================================
    # recall_by_run view - based on grading_edges
    op.execute("""
        CREATE VIEW recall_by_run AS
        WITH grader_stats AS (
            SELECT
                ge.grader_run_id,
                ge.critique_run_id,
                COALESCE(SUM(ge.credit) FILTER (WHERE ge.tp_id IS NOT NULL), 0.0) AS total_credit,
                COUNT(DISTINCT (ge.tp_id, ge.tp_occurrence_id))
                    FILTER (WHERE ge.tp_id IS NOT NULL) AS recall_denominator
            FROM grading_edges ge
            GROUP BY ge.grader_run_id, ge.critique_run_id
        ),
        per_run AS (
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
                compute_stats_with_ci(
                    COALESCE(
                        array_agg(gs.total_credit) FILTER (WHERE cr.status = 'completed'),
                        ARRAY[0.0]::double precision[]
                    )
                ) AS credit_stats
            FROM agent_runs cr
            JOIN examples e ON (
                cr.type_config->'example'->>'snapshot_slug' = e.snapshot_slug
                AND (cr.type_config->'example'->>'kind')::example_kind_enum = e.example_kind
                AND COALESCE((cr.type_config->'example'->>'files_hash'), '') = COALESCE(e.files_hash, '')
            )
            JOIN snapshots s ON cr.type_config->'example'->>'snapshot_slug' = s.slug
            LEFT JOIN grader_stats gs ON gs.critique_run_id = cr.agent_run_id
            WHERE (cr.type_config->>'agent_type') = 'critic'
              AND cr.status != 'in_progress'
            GROUP BY cr.agent_run_id, cr.type_config, cr.image_digest, cr.model, cr.status,
                     e.example_kind, e.files_hash, e.recall_denominator, s.split
        )
        SELECT
            snapshot_slug, example_kind, files_hash, split, recall_denominator,
            critic_run_id, critic_image_digest, critic_model, critic_status,
            credit_stats,
            scale_stats(credit_stats, recall_denominator) AS recall_stats
        FROM per_run
    """)

    op.execute("""
        COMMENT ON VIEW recall_by_run IS
        'Per-critic-run recall using grading_edges. Base view for all recall aggregates.'
    """)

    # recall_by_definition_example view
    op.execute("""
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
                    COALESCE((rbr.credit_stats).mean, 0.0)
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
    """)

    op.execute("""
        COMMENT ON VIEW recall_by_definition_example IS
        'Recall aggregated by (definition, example). Uses grading_edges.'
    """)

    # recall_by_definition_split_kind view
    op.execute("""
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
    """)

    op.execute("""
        COMMENT ON VIEW recall_by_definition_split_kind IS
        'Recall aggregated by (definition, split, example_kind). Uses grading_edges.'
    """)

    # recall_by_example view
    op.execute("""
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
    """)

    op.execute("""
        COMMENT ON VIEW recall_by_example IS
        'Recall aggregated by example (across all definitions). Uses grading_edges.'
    """)

    # pareto_frontier_by_example view
    op.execute("""
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
    """)

    op.execute("""
        COMMENT ON VIEW pareto_frontier_by_example IS
        'Best definitions per example. Uses grading_edges.'
    """)

    # validation_recall_by_definition view
    op.execute("""
        CREATE VIEW validation_recall_by_definition AS
        SELECT
            critic_image_digest,
            critic_model,
            compute_stats_with_ci(array_agg(
                total_credit / NULLIF(n_occurrences, 0)
            )) AS recall_stats
        FROM get_validation_full_snapshot_aggregates()
        GROUP BY critic_image_digest, critic_model
    """)

    op.execute("""
        COMMENT ON VIEW validation_recall_by_definition IS
        'Aggregated validation recall by definition. Uses grading_edges.'
    """)

    # =========================================================================
    # Event and run cost views
    # =========================================================================
    op.execute("""
        CREATE VIEW event_costs AS
        SELECT
            (events.payload->'response_id')::text AS response_id,
            events.agent_run_id,
            ((events.payload->'usage'->'model')::text) AS model,
            ((events.payload->'usage'->'input_tokens')::text)::integer AS input_tokens,
            COALESCE(((events.payload->'usage'->'input_tokens_details'->'cached_tokens')::text)::integer, 0) AS cached_tokens,
            ((events.payload->'usage'->'output_tokens')::text)::integer AS output_tokens,
            COALESCE(((events.payload->'usage'->'output_tokens_details'->'reasoning_tokens')::text)::integer, 0) AS reasoning_tokens,
            (
                (((events.payload->'usage'->'input_tokens')::text)::integer -
                 COALESCE(((events.payload->'usage'->'input_tokens_details'->'cached_tokens')::text)::integer, 0))::float
                * model_metadata.input_usd_per_1m_tokens / 1000000.0
                +
                COALESCE(((events.payload->'usage'->'input_tokens_details'->'cached_tokens')::text)::integer, 0)::float
                * model_metadata.cached_input_usd_per_1m_tokens / 1000000.0
                +
                ((events.payload->'usage'->'output_tokens')::text)::integer::float
                * model_metadata.output_usd_per_1m_tokens / 1000000.0
            ) AS cost_usd,
            events.timestamp
        FROM events
        JOIN model_metadata ON ((events.payload->'usage'->'model')::text) = model_metadata.model_id
        WHERE events.event_type = 'response' AND events.payload->'usage' IS NOT NULL
    """)

    op.execute("""
        COMMENT ON VIEW event_costs IS
        'Per-event cost calculation. Joins events with model_metadata to compute cost_usd.
Extracts token usage from response events and applies pricing from model_metadata.'
    """)

    op.execute("""
        CREATE VIEW run_costs AS
        WITH RECURSIVE run_tree AS (
            -- Base case: the run itself
            SELECT agent_run_id, agent_run_id AS root_run_id
            FROM agent_runs

            UNION ALL

            -- Recursive case: children of runs already in the tree
            SELECT ar.agent_run_id, rt.root_run_id
            FROM agent_runs ar
            JOIN run_tree rt ON ar.parent_agent_run_id = rt.agent_run_id
        )
        SELECT
            rt.root_run_id AS agent_run_id,
            ec.model,
            SUM(ec.input_tokens) AS input_tokens,
            SUM(ec.cached_tokens) AS cached_tokens,
            SUM(ec.output_tokens) AS output_tokens,
            SUM(ec.reasoning_tokens) AS reasoning_tokens,
            SUM(ec.cost_usd) AS cost_usd
        FROM run_tree rt
        JOIN event_costs ec ON ec.agent_run_id = rt.agent_run_id
        GROUP BY rt.root_run_id, ec.model
    """)

    op.execute("""
        COMMENT ON VIEW run_costs IS
        'Aggregated costs per agent run. Includes all transitive child runs via recursive CTE.
Groups by model so queries can see per-model breakdown.'
    """)

    # =========================================================================
    # Occurrence credit views (adapted for grading_edges model)
    # =========================================================================
    op.execute("""
        CREATE VIEW occurrence_credits AS
        -- Graded occurrences: TP occurrences with credits from grading_edges
        SELECT
            -- Example identification
            ge.snapshot_slug,
            s.split,
            ex.example_kind,
            ex.files_hash,
            -- Ground truth
            ge.tp_id,
            ge.tp_occurrence_id AS occurrence_id,
            -- Critic-specific
            ge.critique_run_id AS critic_run_id,
            ge.critique_run_id AS critic_transcript_id,
            cr.image_digest AS critic_image_digest,
            cr.model AS critic_model,
            -- Grader-specific
            ge.grader_run_id,
            ge.grader_run_id AS grader_transcript_id,
            gr.created_at AS graded_at,
            gr.model AS grader_model,
            ge.credit AS found_credit,
            jsonb_build_array(ge.critique_issue_id) AS matched_by_json,
            ge.rationale AS grader_rationale
        FROM grading_edges ge
        JOIN agent_runs cr ON cr.agent_run_id = ge.critique_run_id
        JOIN agent_runs gr ON gr.agent_run_id = ge.grader_run_id
        JOIN snapshots s ON ge.snapshot_slug = s.slug
        JOIN examples ex ON (
            ge.snapshot_slug = ex.snapshot_slug
            AND (cr.type_config->'example'->>'kind')::example_kind_enum = ex.example_kind
            AND COALESCE((cr.type_config->'example'->>'files_hash'), '') = COALESCE(ex.files_hash, '')
        )
        WHERE ge.tp_id IS NOT NULL
          AND (cr.type_config->>'agent_type') = 'critic'

        UNION ALL

        -- Failed critics: generate zero-credit rows for all occurrences in expected recall scope
        SELECT
            (cr.type_config->'example'->>'snapshot_slug') AS snapshot_slug,
            s.split,
            ex.example_kind,
            ex.files_hash,
            tpo.tp_id,
            tpo.occurrence_id,
            cr.agent_run_id AS critic_run_id,
            cr.agent_run_id AS critic_transcript_id,
            cr.image_digest AS critic_image_digest,
            cr.model AS critic_model,
            NULL::uuid AS grader_run_id,
            NULL::uuid AS grader_transcript_id,
            cr.created_at AS graded_at,
            NULL::varchar AS grader_model,
            0.0 AS found_credit,
            NULL::jsonb AS matched_by_json,
            ('Critic failed: ' || cr.status) AS grader_rationale
        FROM agent_runs cr
        JOIN snapshots s ON (cr.type_config->'example'->>'snapshot_slug') = s.slug
        JOIN examples ex ON (
            (cr.type_config->'example'->>'snapshot_slug') = ex.snapshot_slug
            AND (cr.type_config->'example'->>'kind')::example_kind_enum = ex.example_kind
            AND COALESCE((cr.type_config->'example'->>'files_hash'), '') = COALESCE(ex.files_hash, '')
        )
        CROSS JOIN true_positive_occurrences tpo
        WHERE (cr.type_config->>'agent_type') = 'critic'
          AND cr.status = ANY (ARRAY['max_turns_exceeded'::agent_run_status_enum, 'context_length_exceeded'::agent_run_status_enum])
          AND (cr.type_config->'example'->>'snapshot_slug') = tpo.snapshot_slug
          AND is_tp_in_expected_recall_scope(tpo.snapshot_slug, tpo.tp_id, ex.example_kind, ex.files_hash)
    """)

    op.execute("""
        COMMENT ON VIEW occurrence_credits IS
        'Per-occurrence credit from grading_edges. Base view for computing recall.
Each row = one TP occurrence with its found_credit (0-1).
Sum(found_credit)/count(*) = occurrence-weighted recall.

Uses grading_edges model: credits go directly to (tp_id, occurrence_id) pairs.
Failed critics produce zero-credit rows for all occurrences in expected recall scope.
NOTE: Recall can exceed 100%% if critics find issues outside expected scopes.

USEFUL FOR: Prompt optimizer (TRAIN split via RLS), improvement agent.'
    """)

    op.execute("""
        CREATE VIEW occurrence_run_credits AS
        SELECT
            snapshot_slug,
            split,
            example_kind,
            files_hash,
            tp_id,
            occurrence_id,
            critic_run_id,
            critic_transcript_id,
            critic_image_digest,
            critic_model,
            grader_run_id,
            grader_transcript_id,
            graded_at,
            grader_model,
            sum(found_credit) AS total_credit,
            array_agg(DISTINCT matched_by_json) FILTER (WHERE matched_by_json IS NOT NULL) AS all_matched_by,
            string_agg(DISTINCT grader_rationale, ' | ') AS combined_rationale
        FROM occurrence_credits
        GROUP BY snapshot_slug, split, example_kind, files_hash, tp_id, occurrence_id,
            critic_run_id, critic_transcript_id, critic_image_digest, critic_model,
            grader_run_id, grader_transcript_id, graded_at, grader_model
    """)

    op.execute("""
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
            grader_model,
            compute_stats_with_ci(array_agg(total_credit)) AS credit_stats
        FROM occurrence_run_credits
        GROUP BY snapshot_slug, split, example_kind, files_hash, tp_id, occurrence_id,
            critic_image_digest, critic_model, grader_model
    """)

    op.execute("""
        COMMENT ON VIEW occurrence_statistics IS
        'Aggregate statistics per occurrence across all runs.
Uses grading_edges model for credit calculation.

USEFUL FOR: Prompt optimizer, improvement agent.
- Find consistently-missed occurrences (low credit_stats.mean across runs)
- Identify occurrence patterns that need prompt improvements'
    """)

    # =========================================================================
    # 10. Roles and Grants
    # =========================================================================

    # Create agent_base role if not exists
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_base') THEN
                CREATE ROLE agent_base NOLOGIN;
            END IF;
        END
        $$
    """)

    op.execute("GRANT USAGE ON SCHEMA public TO agent_base")
    op.execute("GRANT SELECT,INSERT ON TABLE agent_definitions TO agent_base")
    op.execute("GRANT SELECT ON TABLE agent_runs TO agent_base")
    op.execute("GRANT SELECT ON TABLE examples TO agent_base")
    op.execute("GRANT SELECT ON TABLE file_sets TO agent_base")
    op.execute("GRANT SELECT ON TABLE file_set_members TO agent_base")
    op.execute("GRANT SELECT ON TABLE critic_scopes_expected_to_recall TO agent_base")
    op.execute("GRANT SELECT ON TABLE snapshot_files TO agent_base")
    op.execute("GRANT SELECT ON TABLE true_positive_occurrences TO agent_base")
    op.execute("GRANT SELECT ON TABLE false_positive_occurrences TO agent_base")
    op.execute("GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE grading_edges TO agent_base")
    op.execute("GRANT SELECT ON TABLE snapshots TO agent_base")
    op.execute("GRANT SELECT ON TABLE recall_by_run TO agent_base")
    op.execute("GRANT SELECT ON TABLE recall_by_definition_example TO agent_base")
    op.execute("GRANT SELECT ON TABLE recall_by_definition_split_kind TO agent_base")
    op.execute("GRANT SELECT ON TABLE recall_by_example TO agent_base")
    op.execute("GRANT SELECT ON TABLE events TO agent_base")
    op.execute("GRANT USAGE ON SEQUENCE events_id_seq TO agent_base")
    op.execute("GRANT SELECT ON TABLE false_positives TO agent_base")
    op.execute("GRANT SELECT ON TABLE grading_edge_credit_sums TO agent_base")
    op.execute("GRANT USAGE ON SEQUENCE grading_edges_id_seq TO agent_base")
    op.execute("GRANT SELECT ON TABLE true_positives TO agent_base")
    op.execute("GRANT SELECT ON TABLE occurrence_credits TO agent_base")
    op.execute("GRANT SELECT ON TABLE occurrence_run_credits TO agent_base")
    op.execute("GRANT SELECT ON TABLE occurrence_statistics TO agent_base")
    op.execute("GRANT SELECT ON TABLE pareto_frontier_by_example TO agent_base")
    op.execute("GRANT SELECT ON TABLE validation_recall_by_definition TO agent_base")
    op.execute("GRANT SELECT ON TABLE event_costs TO agent_base")
    op.execute("GRANT SELECT ON TABLE run_costs TO agent_base")
    op.execute("GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE reported_issue_occurrences TO agent_base")
    op.execute("GRANT USAGE ON SEQUENCE reported_issue_occurrences_id_seq TO agent_base")
    op.execute("GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE reported_issues TO agent_base")

    # =========================================================================
    # 11. RLS Policies
    # =========================================================================

    # Force RLS on tables
    op.execute("ALTER TABLE snapshots FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE true_positives FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE false_positives FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reported_issues FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reported_issue_occurrences FORCE ROW LEVEL SECURITY")

    # Enable RLS
    op.execute("ALTER TABLE agent_definitions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE true_positives ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE false_positives ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE true_positive_occurrences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE false_positive_occurrences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE critic_scopes_expected_to_recall ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE grading_edges ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reported_issues ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reported_issue_occurrences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_sets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_set_members ENABLE ROW LEVEL SECURITY")

    # Admin policies for postgres user
    op.execute("CREATE POLICY admin_full_access_events ON events TO postgres USING (true) WITH CHECK (true)")
    op.execute(
        "CREATE POLICY admin_full_access_false_positives ON false_positives TO postgres USING (true) WITH CHECK (true)"
    )
    op.execute("CREATE POLICY admin_full_access_snapshots ON snapshots TO postgres USING (true) WITH CHECK (true)")
    op.execute(
        "CREATE POLICY admin_full_access_true_positives ON true_positives TO postgres USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY admin_full_access_tp_occurrences ON true_positive_occurrences TO postgres USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY admin_full_access_fp_occurrences ON false_positive_occurrences TO postgres USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY admin_full_access_occ_triggers ON critic_scopes_expected_to_recall TO postgres USING (true) WITH CHECK (true)"
    )
    op.execute("CREATE POLICY admin_full_access_file_sets ON file_sets TO postgres USING (true) WITH CHECK (true)")
    op.execute(
        "CREATE POLICY admin_full_access_file_set_members ON file_set_members TO postgres USING (true) WITH CHECK (true)"
    )

    # Agent definitions policies
    op.execute("CREATE POLICY agent_definitions_select ON agent_definitions FOR SELECT USING (true)")
    op.execute(
        "CREATE POLICY agent_definitions_insert ON agent_definitions FOR INSERT WITH CHECK (created_by_agent_run_id = current_agent_run_id())"
    )

    # Agent runs policies (clustering branch removed)
    op.execute("""
        CREATE POLICY agent_runs_agent_select ON agent_runs FOR SELECT USING (
            (current_agent_type() = 'prompt_optimizer'
             AND (((type_config->>'agent_type') = 'critic' AND is_train_snapshot(type_config->'example'->>'snapshot_slug'))
                  OR ((type_config->>'agent_type') = 'grader' AND is_train_snapshot(get_graded_snapshot_slug(agent_run_id)))))
            OR (agent_run_id = current_agent_run_id())
            OR (current_agent_type() = 'grader' AND agent_run_id = current_graded_agent_run_id())
            OR (current_agent_type() = 'improvement'
                AND (type_config->>'agent_type') IN ('critic', 'grader')
                AND is_improvement_example_allowed(type_config->'example'->>'snapshot_slug', (type_config->'example'->>'kind')::example_kind_enum, (type_config->'example'->>'files_hash')))
        )
    """)
    op.execute(
        "CREATE POLICY agent_runs_select_own ON agent_runs FOR SELECT USING (agent_run_id = current_agent_run_id())"
    )
    op.execute(
        "CREATE POLICY agent_runs_select_children ON agent_runs FOR SELECT USING (parent_agent_run_id = current_agent_run_id())"
    )

    # file_sets policies (clustering branch removed)
    op.execute("""
        CREATE POLICY file_sets_agent_select ON file_sets FOR SELECT USING (
            -- Prompt optimizer in whole-repo mode: only TRAIN file_sets
            (current_agent_type() = 'prompt_optimizer'
             AND current_agent_type_config()->>'target_metric' = 'whole_repo'
             AND is_train_snapshot(snapshot_slug))
            -- Prompt optimizer in targeted mode: TRAIN + VALID file_sets
            OR (current_agent_type() = 'prompt_optimizer'
                AND (current_agent_type_config()->>'target_metric' IS NULL
                     OR current_agent_type_config()->>'target_metric' != 'whole_repo')
                AND is_train_or_valid_snapshot(snapshot_slug))
            -- Critic: only their example's file_set
            OR (current_agent_type() = 'critic'
                AND snapshot_slug = current_agent_type_config()->'example'->>'snapshot_slug'
                AND files_hash = current_agent_type_config()->'example'->>'files_hash')
            -- Grader: graded example's file_set
            OR (current_agent_type() = 'grader'
                AND snapshot_slug = get_graded_snapshot_slug(current_agent_run_id()))
            -- Improvement: allowed snapshots only
            OR (current_agent_type() = 'improvement'
                AND is_improvement_snapshot_allowed(snapshot_slug))
        )
    """)

    # file_set_members policies (clustering branch removed)
    op.execute("""
        CREATE POLICY file_set_members_agent_select ON file_set_members FOR SELECT USING (
            -- Prompt optimizer in whole-repo mode: only TRAIN file_set_members
            (current_agent_type() = 'prompt_optimizer'
             AND current_agent_type_config()->>'target_metric' = 'whole_repo'
             AND is_train_snapshot(snapshot_slug))
            -- Prompt optimizer in targeted mode: TRAIN + VALID file_set_members
            OR (current_agent_type() = 'prompt_optimizer'
                AND (current_agent_type_config()->>'target_metric' IS NULL
                     OR current_agent_type_config()->>'target_metric' != 'whole_repo')
                AND is_train_or_valid_snapshot(snapshot_slug))
            -- Critic: own example's file_set_members
            OR (current_agent_type() = 'critic'
                AND snapshot_slug = current_agent_type_config()->'example'->>'snapshot_slug')
            -- Grader: graded snapshot's file_set_members
            OR (current_agent_type() = 'grader'
                AND snapshot_slug = get_graded_snapshot_slug(current_agent_run_id()))
            -- Improvement: allowed snapshots' file_set_members
            OR (current_agent_type() = 'improvement'
                AND is_improvement_snapshot_allowed(snapshot_slug))
        )
    """)

    # Snapshots - any agent with a valid run can see all snapshots metadata
    op.execute(
        "CREATE POLICY snapshots_agent_select ON snapshots FOR SELECT USING (current_agent_run_id() IS NOT NULL)"
    )

    # True positives - uses can_access_snapshot() helper
    op.execute(
        "CREATE POLICY true_positives_agent_select ON true_positives FOR SELECT USING (can_access_snapshot(snapshot_slug))"
    )

    # False positives - uses can_access_snapshot() helper
    op.execute(
        "CREATE POLICY false_positives_agent_select ON false_positives FOR SELECT USING (can_access_snapshot(snapshot_slug))"
    )

    # True positive occurrences - uses can_access_snapshot() helper
    op.execute(
        "CREATE POLICY tp_occurrences_agent_select ON true_positive_occurrences FOR SELECT USING (can_access_snapshot(snapshot_slug))"
    )

    # False positive occurrences - uses can_access_snapshot() helper
    op.execute(
        "CREATE POLICY fp_occurrences_agent_select ON false_positive_occurrences FOR SELECT USING (can_access_snapshot(snapshot_slug))"
    )

    # Occurrence triggers - uses can_access_snapshot() helper
    op.execute(
        "CREATE POLICY occ_triggers_agent_select ON critic_scopes_expected_to_recall FOR SELECT USING (can_access_snapshot(snapshot_slug))"
    )

    # Events policies (clustering branch removed)
    op.execute("""
        CREATE POLICY events_agent_select ON events FOR SELECT USING (
            (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(agent_run_id))
            OR (agent_run_id = current_agent_run_id())
            OR (current_agent_type() = 'improvement'
                AND agent_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)

    # Grading edges policies (uses grader_run_id for ownership, critique_run_id for access control)
    op.execute("""
        CREATE POLICY grading_edges_agent_select ON grading_edges FOR SELECT USING (
            (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(critique_run_id))
            OR is_own_run_as(grader_run_id, 'grader')
            OR (current_agent_type() = 'improvement'
                AND critique_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)
    op.execute(
        "CREATE POLICY grading_edges_agent_insert ON grading_edges FOR INSERT WITH CHECK (is_own_run_as(grader_run_id, 'grader'))"
    )
    op.execute(
        "CREATE POLICY grading_edges_agent_update ON grading_edges FOR UPDATE USING (is_own_run_as(grader_run_id, 'grader'))"
    )
    op.execute(
        "CREATE POLICY grading_edges_agent_delete ON grading_edges FOR DELETE USING (is_own_run_as(grader_run_id, 'grader'))"
    )

    # Reported issues policies
    op.execute("""
        CREATE POLICY reported_issues_agent_select ON reported_issues FOR SELECT USING (
            (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(agent_run_id))
            OR (agent_run_id = current_agent_run_id())
            OR (current_agent_type() = 'grader' AND agent_run_id = get_graded_agent_run_id(current_agent_run_id()))
            OR (current_agent_type() = 'improvement'
                AND agent_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)
    op.execute(
        "CREATE POLICY reported_issues_agent_insert ON reported_issues FOR INSERT WITH CHECK (is_own_run_as(agent_run_id, 'critic'))"
    )
    op.execute(
        "CREATE POLICY reported_issues_agent_update ON reported_issues FOR UPDATE USING (is_own_run_as(agent_run_id, 'critic'))"
    )
    op.execute(
        "CREATE POLICY reported_issues_agent_delete ON reported_issues FOR DELETE USING (is_own_run_as(agent_run_id, 'critic'))"
    )

    # Reported issue occurrences policies
    op.execute("""
        CREATE POLICY reported_issue_occurrences_agent_select ON reported_issue_occurrences FOR SELECT USING (
            (current_agent_type() = 'prompt_optimizer' AND is_train_agent_run(agent_run_id))
            OR (agent_run_id = current_agent_run_id())
            OR (current_agent_type() = 'grader' AND agent_run_id = get_graded_agent_run_id(current_agent_run_id()))
            OR (current_agent_type() = 'improvement'
                AND agent_run_id IN (SELECT get_improvement_allowed_agent_run_ids()))
        )
    """)
    op.execute(
        "CREATE POLICY reported_issue_occurrences_agent_insert ON reported_issue_occurrences FOR INSERT WITH CHECK (is_own_run_as(agent_run_id, 'critic'))"
    )
    op.execute(
        "CREATE POLICY reported_issue_occurrences_agent_update ON reported_issue_occurrences FOR UPDATE USING (is_own_run_as(agent_run_id, 'critic'))"
    )
    op.execute(
        "CREATE POLICY reported_issue_occurrences_agent_delete ON reported_issue_occurrences FOR DELETE USING (is_own_run_as(agent_run_id, 'critic'))"
    )

    # Initialize salt singleton
    op.execute("INSERT INTO agent_role_salt (id) VALUES (1) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    """Drop all schema."""
    # This is a complete schema migration - downgrade drops everything
    op.execute("DROP SCHEMA public CASCADE")
    op.execute("CREATE SCHEMA public")
    op.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
