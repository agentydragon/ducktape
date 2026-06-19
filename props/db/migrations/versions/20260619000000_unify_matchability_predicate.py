"""Unify the match_file_restriction matchability rule into one predicate.

The "matchability" rule — whether a critique issue may be graded against a GT
TP/FP occurrence that carries a ``match_file_restriction`` — was implemented
twice with divergent semantics:

- ``matchable_occurrences()`` (used by the ``grading_pending`` view and workload
  estimation) used OVERLAP: an occurrence is matchable if the restriction is NULL
  or at least one reported file is in the restriction set.
- ``check_edge_matches_filter_scope()`` (BEFORE INSERT/UPDATE trigger on
  ``grading_edges``) used CONTAINMENT: it rejected an edge if ANY reported file
  was outside the restriction set, i.e. it required all reported files ⊆ set.

The documentation (ground_truth.md.mako, grading.md.mako, ground_truth_authoring.md)
uniformly specifies OVERLAP ("only critiques touching at least one file in that
set", "where the critique's files overlap"). The trigger's containment rule is the
bug: a critic that reports a real issue plus an extra adjacent file gets the whole
edge rejected, the grader retries to exhaustion, calls report_failure, exits 1, and
the supervisor respawns it forever (poison pill / crash loop).

Fix: introduce ONE canonical SQL predicate
``occurrence_files_overlap(p_snapshot_slug, p_files_hash, p_files)`` expressing the
overlap rule, and rewrite BOTH ``matchable_occurrences()`` and
``check_edge_matches_filter_scope()`` to call it. Single source of truth.

Revision ID: 20260619000000
Revises: 20260608000000
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260619000000"
down_revision: str | None = "20260608000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Canonical matchability predicate (overlap rule) -------------------------
# Returns TRUE if an occurrence with the given match_file_restriction file set
# is matchable from the given reported files:
#   - NULL restriction  -> unrestricted, always matchable
#   - non-NULL          -> matchable iff at least one reported file is in the set
OVERLAP_PREDICATE = """
    CREATE OR REPLACE FUNCTION occurrence_files_overlap(
        p_snapshot_slug VARCHAR,
        p_files_hash VARCHAR,
        p_files VARCHAR[]
    ) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path = public
    AS $$
        SELECT p_files_hash IS NULL
            OR EXISTS (
                SELECT 1 FROM file_set_members fsm
                WHERE fsm.snapshot_slug = p_snapshot_slug
                  AND fsm.files_hash = p_files_hash
                  AND fsm.file_path = ANY(p_files)
            )
    $$
"""

# Signature unchanged -> CREATE OR REPLACE keeps the grading_pending view (and any
# other dependents) bound without a CASCADE drop.
MATCHABLE_OCCURRENCES_UNIFIED = """
    CREATE OR REPLACE FUNCTION matchable_occurrences(
        p_snapshot_slug VARCHAR,
        p_files VARCHAR[]
    ) RETURNS TABLE (
        tp_id VARCHAR,
        tp_occurrence_id VARCHAR,
        fp_id VARCHAR,
        fp_occurrence_id VARCHAR
    ) AS $$
        SELECT tpo.tp_id, tpo.occurrence_id, NULL::VARCHAR, NULL::VARCHAR
        FROM true_positive_occurrences tpo
        WHERE tpo.snapshot_slug = p_snapshot_slug
          AND occurrence_files_overlap(tpo.snapshot_slug, tpo.match_file_restriction, p_files)
        UNION ALL
        SELECT NULL, NULL, fpo.fp_id, fpo.occurrence_id
        FROM false_positive_occurrences fpo
        WHERE fpo.snapshot_slug = p_snapshot_slug
          AND occurrence_files_overlap(fpo.snapshot_slug, fpo.match_file_restriction, p_files)
    $$ LANGUAGE SQL STABLE
"""

# Trigger now enforces the SAME overlap rule: reject only when the restriction is
# set and NONE of the critique's reported files fall inside it.
CHECK_EDGE_FILTER_SCOPE_UNIFIED = """
    CREATE OR REPLACE FUNCTION check_edge_matches_filter_scope() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public
    AS $$
    DECLARE
        filter_hash TEXT;
        reported_files VARCHAR[];
    BEGIN
        IF NEW.tp_id IS NOT NULL THEN
            SELECT match_file_restriction INTO filter_hash
            FROM true_positive_occurrences
            WHERE snapshot_slug = NEW.snapshot_slug
              AND tp_id = NEW.tp_id
              AND occurrence_id = NEW.tp_occurrence_id;
        ELSE
            SELECT match_file_restriction INTO filter_hash
            FROM false_positive_occurrences
            WHERE snapshot_slug = NEW.snapshot_slug
              AND fp_id = NEW.fp_id
              AND occurrence_id = NEW.fp_occurrence_id;
        END IF;

        IF filter_hash IS NULL THEN
            RETURN NEW;
        END IF;

        -- Files this critique issue reported (reported_issue_occurrences.locations
        -- is a JSONB array of {file, start_line?, end_line?}).
        SELECT array_agg(DISTINCT loc->>'file')
        INTO reported_files
        FROM reported_issue_occurrences rio
        CROSS JOIN LATERAL jsonb_array_elements(rio.locations) AS loc
        WHERE rio.agent_run_id = NEW.critique_run_id
          AND rio.reported_issue_id = NEW.critique_issue_id
          AND loc->>'file' IS NOT NULL;

        IF NOT occurrence_files_overlap(NEW.snapshot_slug, filter_hash, COALESCE(reported_files, ARRAY[]::VARCHAR[])) THEN
            RAISE EXCEPTION 'Critique issue % reports no files overlapping target occurrence match_file_restriction scope (filter: %)',
                NEW.critique_issue_id, filter_hash;
        END IF;

        RETURN NEW;
    END;
    $$
"""

# --- Original (buggy) definitions, restored on downgrade ---------------------
MATCHABLE_OCCURRENCES_ORIGINAL = """
    CREATE OR REPLACE FUNCTION matchable_occurrences(
        p_snapshot_slug VARCHAR,
        p_files VARCHAR[]
    ) RETURNS TABLE (
        tp_id VARCHAR,
        tp_occurrence_id VARCHAR,
        fp_id VARCHAR,
        fp_occurrence_id VARCHAR
    ) AS $$
        SELECT tpo.tp_id, tpo.occurrence_id, NULL::VARCHAR, NULL::VARCHAR
        FROM true_positive_occurrences tpo
        WHERE tpo.snapshot_slug = p_snapshot_slug
          AND (
              tpo.match_file_restriction IS NULL
              OR EXISTS (
                  SELECT 1 FROM file_set_members fsm
                  WHERE fsm.snapshot_slug = tpo.snapshot_slug
                    AND fsm.files_hash = tpo.match_file_restriction
                    AND fsm.file_path = ANY(p_files)
              )
          )
        UNION ALL
        SELECT NULL, NULL, fpo.fp_id, fpo.occurrence_id
        FROM false_positive_occurrences fpo
        WHERE fpo.snapshot_slug = p_snapshot_slug
          AND (
              fpo.match_file_restriction IS NULL
              OR EXISTS (
                  SELECT 1 FROM file_set_members fsm
                  WHERE fsm.snapshot_slug = fpo.snapshot_slug
                    AND fsm.files_hash = fpo.match_file_restriction
                    AND fsm.file_path = ANY(p_files)
              )
          )
    $$ LANGUAGE SQL STABLE
"""

CHECK_EDGE_FILTER_SCOPE_ORIGINAL = """
    CREATE OR REPLACE FUNCTION check_edge_matches_filter_scope() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        filter_hash TEXT;
    BEGIN
        IF NEW.tp_id IS NOT NULL THEN
            SELECT match_file_restriction INTO filter_hash
            FROM true_positive_occurrences
            WHERE snapshot_slug = NEW.snapshot_slug
              AND tp_id = NEW.tp_id
              AND occurrence_id = NEW.tp_occurrence_id;
        ELSE
            SELECT match_file_restriction INTO filter_hash
            FROM false_positive_occurrences
            WHERE snapshot_slug = NEW.snapshot_slug
              AND fp_id = NEW.fp_id
              AND occurrence_id = NEW.fp_occurrence_id;
        END IF;

        IF filter_hash IS NULL THEN
            RETURN NEW;
        END IF;

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
            RAISE EXCEPTION 'Critique issue % reports files outside target occurrence match_file_restriction scope (filter: %)',
                NEW.critique_issue_id, filter_hash;
        END IF;

        RETURN NEW;
    END;
    $$
"""


def upgrade() -> None:
    op.execute(OVERLAP_PREDICATE)
    op.execute(MATCHABLE_OCCURRENCES_UNIFIED)
    op.execute(CHECK_EDGE_FILTER_SCOPE_UNIFIED)
    op.execute("""
        COMMENT ON FUNCTION occurrence_files_overlap(VARCHAR, VARCHAR, VARCHAR[]) IS
        'Canonical match_file_restriction matchability rule (OVERLAP).
TRUE if p_files_hash IS NULL (unrestricted) or at least one of p_files is in the
restriction file set. Single source of truth shared by matchable_occurrences()
(grading_pending view, workload estimation) and the enforce_edge_filter_scope trigger.'
    """)


def downgrade() -> None:
    op.execute(MATCHABLE_OCCURRENCES_ORIGINAL)
    op.execute(CHECK_EDGE_FILTER_SCOPE_ORIGINAL)
    op.execute("DROP FUNCTION IF EXISTS occurrence_files_overlap(VARCHAR, VARCHAR, VARCHAR[])")
