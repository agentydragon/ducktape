"""Conversion functions from ORM models to database persistence models.

This module converts ORM models (db.models.TruePositive, FalsePositive) to
JSON-serializable persistence models (db.snapshots.DBTruePositiveIssue, DBKnownFalsePositive).
"""

from __future__ import annotations

from collections import defaultdict

from props.core.db.models import FalsePositive, OccurrenceRangeORM, TruePositive
from props.core.db.snapshots import (
    DBFalsePositiveOccurrence,
    DBKnownFalsePositive,
    DBLineRange,
    DBTruePositiveIssue,
    DBTruePositiveOccurrence,
)


def _convert_orm_ranges_to_db_files(ranges: list[OccurrenceRangeORM]) -> dict[str, list[DBLineRange]]:
    """Convert ORM ranges to DB persistence model files dict.

    Groups ranges by file path (as strings) and converts to DBLineRange objects.
    """
    by_file: dict[str, list[DBLineRange]] = defaultdict(list)
    for range_orm in ranges:
        by_file[str(range_orm.file_path)].append(
            DBLineRange(start_line=range_orm.start_line, end_line=range_orm.end_line, note=range_orm.note)
        )
    return dict(by_file) if by_file else {}


def orm_tp_to_db(orm_tp: TruePositive) -> DBTruePositiveIssue:
    """Convert ORM TruePositive to DB persistence model."""
    return DBTruePositiveIssue(
        id=orm_tp.tp_id,
        rationale=orm_tp.rationale,
        occurrences=[
            DBTruePositiveOccurrence(
                occurrence_id=occ.occurrence_id,
                files=_convert_orm_ranges_to_db_files(occ.ranges),
                note=occ.note,
                critic_scopes_expected_to_recall=[
                    [str(p) for p in trigger_set] for trigger_set in occ.critic_scopes_expected_to_recall_set
                ],
            )
            for occ in orm_tp.occurrences
        ],
    )


def orm_fp_to_db(orm_fp: FalsePositive) -> DBKnownFalsePositive:
    """Convert ORM FalsePositive to DB persistence model."""
    return DBKnownFalsePositive(
        id=orm_fp.fp_id,
        rationale=orm_fp.rationale,
        occurrences=[
            DBFalsePositiveOccurrence(
                occurrence_id=occ.occurrence_id,
                files=_convert_orm_ranges_to_db_files(occ.ranges),
                note=occ.note,
                relevant_files=[str(rf.file_path) for rf in occ.relevant_file_orms],
            )
            for occ in orm_fp.occurrences
        ],
    )
