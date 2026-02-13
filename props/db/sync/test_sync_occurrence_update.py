"""Tests for occurrence-level sync diffing (update path).

Verifies that _tp_occ_from_orm/_fp_occ_from_orm round-trip correctly and that
_sync_tp_issue/_sync_fp_issue detect field-level changes, add/remove occurrences,
and preserve unchanged rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.core.models.true_positive import FalsePositiveOccurrence, LineRange, TruePositiveOccurrence
from props.db.database import Database
from props.db.models import FalsePositive, FalsePositiveOccurrenceORM, TruePositive, TruePositiveOccurrenceORM
from props.db.sync.sync import _fp_occ_from_orm, _sync_fp_issue, _sync_tp_issue, _tp_occ_from_orm
from props.db.sync.yaml_loader import FalsePositive as FalsePositive_yaml, TruePositive as TruePositive_yaml

SLUG = SnapshotSlug("test-fixtures/train1")


# ---------------------------------------------------------------------------
# Round-trip: ORM → Pydantic reconstruction
# ---------------------------------------------------------------------------


def test_tp_occ_round_trip(synced_db: Database):
    """_tp_occ_from_orm produces a Pydantic model matching the original YAML data."""
    with synced_db.session() as session:
        # tp-006 has two occurrences with distinct scopes and match_file_restriction
        db_occ = (
            session.query(TruePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, tp_id="tp-006", occurrence_id="occ-add")
            .one()
        )
        pydantic_occ = _tp_occ_from_orm(db_occ)

        assert pydantic_occ.occurrence_id == "occ-add"
        assert pydantic_occ.note == "Occurrence in add.py"
        assert pydantic_occ.files == {Path("add.py"): [LineRange(start_line=4, end_line=6, note=None)]}
        assert pydantic_occ.critic_scopes_expected_to_recall == {frozenset({Path("add.py")})}
        assert pydantic_occ.match_file_restriction == {Path("add.py")}


def test_fp_occ_round_trip(synced_db: Database):
    """_fp_occ_from_orm produces a Pydantic model matching the original YAML data."""
    with synced_db.session() as session:
        db_occ = (
            session.query(FalsePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, fp_id="fp-001", occurrence_id="fp-occ-1")
            .one()
        )
        pydantic_occ = _fp_occ_from_orm(db_occ)

        assert pydantic_occ.occurrence_id == "fp-occ-1"
        assert pydantic_occ.files == {Path("subtract.py"): [LineRange(start_line=5, end_line=5, note=None)]}
        assert pydantic_occ.relevant_files == {Path("subtract.py")}


# ---------------------------------------------------------------------------
# No-change re-sync preserves rows (created_at unchanged)
# ---------------------------------------------------------------------------


def test_unchanged_occurrence_preserved(synced_db: Database):
    """Re-syncing identical data preserves existing ORM rows (no delete+re-add)."""
    with synced_db.session() as session:
        db_occ = (
            session.query(TruePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, tp_id="tp-006", occurrence_id="occ-add")
            .one()
        )
        original_created_at = db_occ.created_at

        # Re-sync with identical data
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-006").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        assert not changed

        session.refresh(db_occ)
        assert db_occ.created_at == original_created_at


# ---------------------------------------------------------------------------
# Field-level change detection
# ---------------------------------------------------------------------------


def test_note_change_detected(synced_db: Database):
    """Changing an occurrence's note triggers re-sync."""
    with synced_db.session() as session:
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-006").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        # Mutate the note on one occurrence
        occ = _find_occ(yaml_issue.occurrences, "occ-add")
        yaml_issue.occurrences = [
            TruePositiveOccurrence(
                occurrence_id=occ.occurrence_id,
                files=occ.files,
                note="Changed note",
                critic_scopes_expected_to_recall=occ.critic_scopes_expected_to_recall,
                match_file_restriction=occ.match_file_restriction,
            )
            if o.occurrence_id == "occ-add"
            else o
            for o in yaml_issue.occurrences
        ]

        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        assert changed
        refreshed = (
            session.query(TruePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, tp_id="tp-006", occurrence_id="occ-add")
            .one()
        )
        assert refreshed.note == "Changed note"


def test_files_change_detected(synced_db: Database):
    """Changing an occurrence's file ranges triggers re-sync."""
    with synced_db.session() as session:
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-001").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        # Change line range
        yaml_issue.occurrences = [
            TruePositiveOccurrence(
                occurrence_id="occ-1",
                files={Path("subtract.py"): [LineRange(start_line=10, end_line=20, note=None)]},
                note=yaml_issue.occurrences[0].note,
                critic_scopes_expected_to_recall=yaml_issue.occurrences[0].critic_scopes_expected_to_recall,
                match_file_restriction=yaml_issue.occurrences[0].match_file_restriction,
            )
        ]

        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        assert changed
        # Verify ranges via ORM directly (critic_scopes are synced in a separate phase,
        # so _tp_occ_from_orm would fail validation on the freshly re-added occurrence)
        db_occ = (
            session.query(TruePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, tp_id="tp-001", occurrence_id="occ-1")
            .one()
        )
        assert len(db_occ.ranges) == 1
        r = db_occ.ranges[0]
        assert str(r.file_path) == "subtract.py"
        assert r.start_line == 10
        assert r.end_line == 20


def test_rationale_change_detected(synced_db: Database):
    """Changing the issue-level rationale triggers update."""
    with synced_db.session() as session:
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-001").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        yaml_issue.rationale = "Updated rationale."

        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        assert changed
        session.refresh(existing)
        assert existing.rationale == "Updated rationale."


def test_critic_scopes_change_detected(synced_db: Database):
    """Changing critic_scopes_expected_to_recall triggers re-sync (detected as changed)."""
    with synced_db.session() as session:
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-001").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        original_created_at = existing.occurrences[0].created_at
        occ = yaml_issue.occurrences[0]
        yaml_issue.occurrences = [
            TruePositiveOccurrence(
                occurrence_id=occ.occurrence_id,
                files=occ.files,
                note=occ.note,
                # Add a second scope alternative
                critic_scopes_expected_to_recall=occ.critic_scopes_expected_to_recall | {frozenset({Path("add.py")})},
                match_file_restriction=occ.match_file_restriction,
            )
        ]

        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        # The change should be detected; the occurrence is deleted+re-added
        # (critic_scopes_expected_to_recall rows are populated later by sync_file_sets_to_db)
        assert changed
        db_occ = (
            session.query(TruePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, tp_id="tp-001", occurrence_id="occ-1")
            .one()
        )
        assert db_occ.created_at != original_created_at


# ---------------------------------------------------------------------------
# Occurrence add / remove
# ---------------------------------------------------------------------------


def test_occurrence_added(synced_db: Database):
    """Adding an occurrence to an existing issue is detected."""
    with synced_db.session() as session:
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-001").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        yaml_issue.occurrences.append(
            TruePositiveOccurrence(
                occurrence_id="occ-new",
                files={Path("add.py"): [LineRange(start_line=1, end_line=3, note=None)]},
                note="New occurrence",
                critic_scopes_expected_to_recall={frozenset({Path("add.py")})},
                match_file_restriction=None,
            )
        )

        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        assert changed
        occ_ids = {
            o.occurrence_id
            for o in session.query(TruePositiveOccurrenceORM).filter_by(snapshot_slug=SLUG, tp_id="tp-001").all()
        }
        assert occ_ids == {"occ-1", "occ-new"}


def test_occurrence_removed(synced_db: Database):
    """Removing an occurrence from an existing issue is detected."""
    with synced_db.session() as session:
        existing = session.query(TruePositive).filter_by(snapshot_slug=SLUG, tp_id="tp-006").one()
        yaml_issue = _tp_issue_from_orm(session, existing)
        # Keep only the first occurrence
        yaml_issue.occurrences = [yaml_issue.occurrences[0]]

        changed = _sync_tp_issue(session, existing, yaml_issue)
        session.flush()

        assert changed
        occ_ids = {
            o.occurrence_id
            for o in session.query(TruePositiveOccurrenceORM).filter_by(snapshot_slug=SLUG, tp_id="tp-006").all()
        }
        assert len(occ_ids) == 1


# ---------------------------------------------------------------------------
# FP-specific: relevant_files change
# ---------------------------------------------------------------------------


def test_fp_relevant_files_change_detected(synced_db: Database):
    """Changing relevant_files on an FP occurrence triggers re-sync."""
    with synced_db.session() as session:
        existing = session.query(FalsePositive).filter_by(snapshot_slug=SLUG, fp_id="fp-001").one()
        yaml_fp = _fp_issue_from_orm(session, existing)
        occ = yaml_fp.occurrences[0]
        yaml_fp.occurrences = [
            FalsePositiveOccurrence(
                occurrence_id=occ.occurrence_id,
                files=occ.files,
                note=occ.note,
                relevant_files=occ.relevant_files | {Path("add.py")},
                match_file_restriction=occ.match_file_restriction,
            )
        ]

        changed = _sync_fp_issue(session, existing, yaml_fp)
        session.flush()

        assert changed
        db_occ = (
            session.query(FalsePositiveOccurrenceORM)
            .filter_by(snapshot_slug=SLUG, fp_id="fp-001", occurrence_id="fp-occ-1")
            .one()
        )
        reconstructed = _fp_occ_from_orm(db_occ)
        assert Path("add.py") in reconstructed.relevant_files


# ---------------------------------------------------------------------------
# Helpers: build yaml bridge objects from current DB state
# ---------------------------------------------------------------------------


def _find_occ(occs: list[TruePositiveOccurrence], occ_id: str) -> TruePositiveOccurrence:
    return next(o for o in occs if o.occurrence_id == occ_id)


def _tp_issue_from_orm(session: Session, tp: TruePositive) -> TruePositive_yaml:
    """Build a TruePositive_yaml from current DB state (for mutation tests)."""
    return TruePositive_yaml(
        tp_id=tp.tp_id,
        snapshot_slug=tp.snapshot_slug,
        rationale=tp.rationale,
        occurrences=[_tp_occ_from_orm(o) for o in tp.occurrences],
    )


def _fp_issue_from_orm(session: Session, fp: FalsePositive) -> FalsePositive_yaml:
    """Build a FalsePositive_yaml from current DB state (for mutation tests)."""
    return FalsePositive_yaml(
        fp_id=fp.fp_id,
        snapshot_slug=fp.snapshot_slug,
        rationale=fp.rationale,
        occurrences=[_fp_occ_from_orm(o) for o in fp.occurrences],
    )


if __name__ == "__main__":
    pytest_bazel.main()
