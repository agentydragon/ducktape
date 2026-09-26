"""Allow nullable start_line and end_line on occurrence_ranges.

Supports occurrences with unspecified line anchors (None file anchor) where
both start_line and end_line are NULL, and single-line anchors where end_line
is NULL.

Revision ID: 20260926000000
Revises: 20260809000000
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260926000000"
down_revision: str | None = "20260809000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("occurrence_ranges", "start_line", existing_type=sa.Integer(), nullable=True)
    op.alter_column("occurrence_ranges", "end_line", existing_type=sa.Integer(), nullable=True)

    op.drop_constraint("occurrence_range_start_line_positive", "occurrence_ranges", type_="check")
    op.drop_constraint("occurrence_range_end_gte_start", "occurrence_ranges", type_="check")

    op.create_check_constraint(
        "occurrence_range_start_line_positive",
        "occurrence_ranges",
        "start_line IS NULL OR start_line >= 1",
    )
    op.create_check_constraint(
        "occurrence_range_end_gte_start",
        "occurrence_ranges",
        "(start_line IS NULL AND end_line IS NULL) OR (start_line IS NOT NULL AND (end_line IS NULL OR end_line >= start_line))",
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION validate_range_line_numbers()
        RETURNS TRIGGER AS $$
        DECLARE
            file_line_count INT;
            effective_end_line INT;
        BEGIN
            -- Unspecified line anchor: no range bounds to validate
            IF NEW.start_line IS NULL THEN
                RETURN NEW;
            END IF;

            -- Get line count for the referenced file
            SELECT line_count INTO file_line_count
            FROM snapshot_files
            WHERE snapshot_slug = NEW.snapshot_slug
              AND file_path = NEW.file_path;

            effective_end_line := COALESCE(NEW.end_line, NEW.start_line);

            -- Validate line range does not exceed file length
            IF effective_end_line > file_line_count THEN
                RAISE EXCEPTION 'Line range [%, %] exceeds file line count % for file % in snapshot %',
                    NEW.start_line, NEW.end_line, file_line_count, NEW.file_path, NEW.snapshot_slug;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_range_line_numbers()
        RETURNS TRIGGER AS $$
        DECLARE
            file_line_count INT;
        BEGIN
            SELECT line_count INTO file_line_count
            FROM snapshot_files
            WHERE snapshot_slug = NEW.snapshot_slug
              AND file_path = NEW.file_path;

            IF NEW.end_line > file_line_count THEN
                RAISE EXCEPTION 'Line range [%, %] exceeds file line count % for file % in snapshot %',
                    NEW.start_line, NEW.end_line, file_line_count, NEW.file_path, NEW.snapshot_slug;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.drop_constraint("occurrence_range_end_gte_start", "occurrence_ranges", type_="check")
    op.drop_constraint("occurrence_range_start_line_positive", "occurrence_ranges", type_="check")

    op.create_check_constraint(
        "occurrence_range_start_line_positive",
        "occurrence_ranges",
        "start_line >= 1",
    )
    op.create_check_constraint(
        "occurrence_range_end_gte_start",
        "occurrence_ranges",
        "end_line >= start_line",
    )

    op.alter_column("occurrence_ranges", "end_line", existing_type=sa.Integer(), nullable=False)
    op.alter_column("occurrence_ranges", "start_line", existing_type=sa.Integer(), nullable=False)
