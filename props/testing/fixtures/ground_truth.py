"""Ground truth fixtures (TPs, FPs, occurrences) for props tests."""

import pytest
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.db.examples import Example
from props.db.models import FalsePositiveOccurrenceORM, FileSet, FileSetMember, TruePositiveOccurrenceORM


def get_tp_occurrences_for_snapshot(snapshot_slug: str, session: Session) -> list[tuple[str, str]]:
    """Get all TP occurrence (tp_id, occurrence_id) tuples for a snapshot."""
    rows = (
        session.query(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .filter_by(snapshot_slug=snapshot_slug)
        .order_by(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .all()
    )
    return [(row.tp_id, row.occurrence_id) for row in rows]


# ---------------------------------------------------------------------------
# ORM fixtures for real TP/FP occurrences from git fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def example_subtract_orm(synced_test_session: Session) -> Example:
    """ORM Example for subtract.py file-set from git-synced fixture."""
    slug = SnapshotSlug("test-fixtures/train1")
    example = (
        synced_test_session.query(Example)
        .join(FileSet, (FileSet.snapshot_slug == Example.snapshot_slug) & (FileSet.files_hash == Example.files_hash))
        .join(FileSetMember)
        .filter(Example.snapshot_slug == slug)
        .filter(Example.files_hash.isnot(None))
        .filter(FileSetMember.file_path == "subtract.py")
        .first()
    )
    assert example is not None, "Expected file-set example for subtract.py in train1"
    return example


@pytest.fixture
def example_multi_tp_orm(synced_test_session: Session) -> Example:
    """ORM Example with multiple TP occurrences from git-synced fixture (e.g., add.py)."""
    slug = SnapshotSlug("test-fixtures/train1")
    example = (
        synced_test_session.query(Example)
        .filter_by(snapshot_slug=slug)
        .filter(Example.files_hash.isnot(None))
        .filter(Example.recall_denominator > 1)
        .first()
    )
    assert example is not None, "Expected file-set example with multiple TP occurrences in train1"
    return example


@pytest.fixture
def tp_occurrence_single(synced_test_session: Session, example_subtract_orm: Example) -> tuple[str, str]:
    """(tp_id, occurrence_id) for a real single-occurrence TP in train1."""
    tp_occ = (
        synced_test_session.query(TruePositiveOccurrenceORM)
        .filter_by(snapshot_slug=example_subtract_orm.snapshot_slug)
        .order_by(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .first()
    )
    assert tp_occ is not None, "Expected at least one TP occurrence in train1"
    return tp_occ.tp_id, tp_occ.occurrence_id


@pytest.fixture
def tp_occurrences_multi(synced_test_session: Session, example_multi_tp_orm: Example) -> list[tuple[str, str]]:
    """List of (tp_id, occurrence_id) for a multi-occurrence example in train1."""
    rows = (
        synced_test_session.query(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .filter_by(snapshot_slug=example_multi_tp_orm.snapshot_slug)
        .order_by(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .all()
    )
    assert rows, "Expected TP occurrences for multi-TP example in train1"
    return [(row.tp_id, row.occurrence_id) for row in rows]


@pytest.fixture
def fp_occurrence(synced_test_session: Session) -> tuple[str, str]:
    """FP occurrence (fp_id, occurrence_id) from git fixtures (fail fast if missing)."""
    row = (
        synced_test_session.query(FalsePositiveOccurrenceORM.fp_id, FalsePositiveOccurrenceORM.occurrence_id)
        .order_by(FalsePositiveOccurrenceORM.fp_id, FalsePositiveOccurrenceORM.occurrence_id)
        .first()
    )
    assert row is not None, "Expected at least one FP occurrence in git fixtures"
    return row.fp_id, row.occurrence_id


@pytest.fixture
def fp_id(fp_occurrence: tuple[str, str]) -> str:
    """FP id from git fixtures."""
    return fp_occurrence[0]


@pytest.fixture
def fp_occurrence_id(fp_occurrence: tuple[str, str]) -> str:
    """FP occurrence id from git fixtures."""
    return fp_occurrence[1]
