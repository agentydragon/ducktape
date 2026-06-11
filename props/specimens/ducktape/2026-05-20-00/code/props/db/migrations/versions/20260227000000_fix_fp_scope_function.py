"""Fix is_fp_relevant_for_scope to use normalized tables.

The function was created with check_function_bodies=false and references
false_positives.occurrences (a JSONB column that never existed in the
normalized schema). The file_set branch uses jsonb_array_elements() on this
non-existent column, causing ProgrammingError when PostgreSQL inlines the
function body — even for whole_snapshot calls.

Fix: rewrite to use the normalized fp_occurrence_relevant_files table.
Also add SET search_path = public for PG 18 compatibility (same as
is_tp_in_expected_recall_scope in 20260224000000).

Revision ID: 20260227000000
Revises: 20260226000000
Create Date: 2026-02-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260227000000"
down_revision: str | None = "20260226000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIXED_FUNCTION = """
    CREATE OR REPLACE FUNCTION is_fp_relevant_for_scope(
        p_snapshot_slug text,
        p_fp_id text,
        p_example_kind example_kind_enum,
        p_files_hash text
    ) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path = public
    AS $$
        SELECT CASE
            WHEN p_example_kind = 'whole_snapshot' THEN TRUE
            ELSE EXISTS (
                SELECT 1
                FROM fp_occurrence_relevant_files frf
                WHERE frf.snapshot_slug = p_snapshot_slug
                  AND frf.fp_id = p_fp_id
                  AND frf.file_path IN (
                      SELECT fsm.file_path
                      FROM file_set_members fsm
                      WHERE fsm.snapshot_slug = p_snapshot_slug
                        AND fsm.files_hash = p_files_hash
                  )
            )
        END
    $$
"""

ORIGINAL_FUNCTION = """
    CREATE OR REPLACE FUNCTION is_fp_relevant_for_scope(
        p_snapshot_slug text,
        p_fp_id text,
        p_example_kind example_kind_enum,
        p_files_hash text
    ) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
        SELECT CASE
            WHEN p_example_kind = 'whole_snapshot' THEN TRUE
            ELSE EXISTS (
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
"""


def upgrade() -> None:
    op.execute(FIXED_FUNCTION)


def downgrade() -> None:
    # Restore the original (broken) function with check_function_bodies disabled
    # so the reference to the non-existent column doesn't block the downgrade.
    op.execute("SET check_function_bodies = false")
    op.execute(ORIGINAL_FUNCTION)
    op.execute("SET check_function_bodies = true")
