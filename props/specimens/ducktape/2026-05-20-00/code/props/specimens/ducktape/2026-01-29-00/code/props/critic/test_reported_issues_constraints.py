"""Test SQL constraints on reported_issues and reported_issue_occurrences tables.

Verifies that database constraints correctly enforce:
- Locations array must be non-empty
- Each location must have a file field
- Valid line ranges (start_line >= 1, end_line >= 1, end_line >= start_line)
- Primary key uniqueness (no duplicate issue_id per run)
"""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from props.core.ids import SnapshotSlug
from props.db.examples import Example
from props.db.models import ReportedIssue, ReportedIssueOccurrence
from props.db.session import get_session
from props.db.snapshots import DBLocationAnchor
from props.testing.fixtures.runs import make_critic_run

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def test_critic_run(synced_test_db):
    """Create a test critic run using synced fixtures."""
    with get_session() as session:
        # Query an existing example from test fixtures (synced_test_db provides agent definitions)
        example = session.query(Example).filter_by(snapshot_slug=SnapshotSlug("test-fixtures/train1")).first()
        assert example is not None, "Expected test-fixtures/train1 example to exist"

        # Create critic run
        critic_run = make_critic_run(example=example)
        session.add(critic_run)
        session.commit()

        # Return ID while session is still open (SQLAlchemy session scoping)
        return critic_run.agent_run_id


def test_occurrence_single_location_valid(test_critic_run):
    """Valid: occurrence with single location."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-1", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Valid: single location with file and line range (use real fixture file)
        occ = ReportedIssueOccurrence(
            agent_run_id=test_critic_run,
            reported_issue_id="test-issue-1",
            locations=[DBLocationAnchor(file="add.py", start_line=1, end_line=3)],
        )
        session.add(occ)
        session.commit()  # Should succeed


def test_occurrence_single_location_whole_file_valid(test_critic_run):
    """Valid: occurrence with file-level location (no line range)."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-2", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Valid: whole-file location (no line numbers, use real fixture file)
        occ = ReportedIssueOccurrence(
            agent_run_id=test_critic_run,
            reported_issue_id="test-issue-2",
            locations=[DBLocationAnchor(file="subtract.py")],
        )
        session.add(occ)
        session.commit()  # Should succeed


def test_occurrence_multiple_locations_valid(test_critic_run):
    """Valid: occurrence with multiple locations (e.g., duplicated code)."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-3", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Valid: multiple locations (cross-file duplication, use real fixture files)
        occ = ReportedIssueOccurrence(
            agent_run_id=test_critic_run,
            reported_issue_id="test-issue-3",
            locations=[
                DBLocationAnchor(file="multiply.py", start_line=1, end_line=3),
                DBLocationAnchor(file="divide.py", start_line=1, end_line=3),
            ],
        )
        session.add(occ)
        session.commit()  # Should succeed


def test_occurrence_empty_locations_invalid(test_critic_run):
    """Invalid: occurrence with empty locations array."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-4", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Invalid: empty locations array
        occ = ReportedIssueOccurrence(
            agent_run_id=test_critic_run,
            reported_issue_id="test-issue-4",
            locations=[],  # Empty
        )
        session.add(occ)

        # Should fail CHECK constraint (locations must be non-empty)
        with pytest.raises(IntegrityError, match="locations_not_empty"):
            session.commit()

        # Rollback to clean up the failed transaction
        session.rollback()


def test_line_range_start_line_zero_invalid(test_critic_run):
    """Invalid: start_line = 0 (must be >= 1)."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-6", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Invalid: start_line = 0 (Pydantic validation should catch this)
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            ReportedIssueOccurrence(
                agent_run_id=test_critic_run,
                reported_issue_id="test-issue-6",
                locations=[DBLocationAnchor(file="add.py", start_line=0)],
            )


def test_line_range_start_line_negative_invalid(test_critic_run):
    """Invalid: start_line < 0."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-7", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Invalid: negative start_line (Pydantic validation should catch this)
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            ReportedIssueOccurrence(
                agent_run_id=test_critic_run,
                reported_issue_id="test-issue-7",
                locations=[DBLocationAnchor(file="add.py", start_line=-5)],
            )


def test_line_range_end_line_zero_invalid(test_critic_run):
    """Invalid: end_line = 0 (must be >= 1)."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-8", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Invalid: end_line = 0 (Pydantic validation should catch this)
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            ReportedIssueOccurrence(
                agent_run_id=test_critic_run,
                reported_issue_id="test-issue-8",
                locations=[DBLocationAnchor(file="add.py", start_line=1, end_line=0)],
            )


def test_line_range_valid_single_line(test_critic_run):
    """Valid: start_line = end_line (single line)."""
    with get_session() as session:
        # Create issue
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="test-issue-9", rationale="Test issue")
        session.add(issue)
        session.flush()

        # Valid: single line (use real fixture file)
        occ = ReportedIssueOccurrence(
            agent_run_id=test_critic_run,
            reported_issue_id="test-issue-9",
            locations=[DBLocationAnchor(file="add.py", start_line=1, end_line=1)],
        )
        session.add(occ)
        session.commit()  # Should succeed


def test_duplicate_issue_id_not_allowed(test_critic_run):
    """Cannot have two issues with same ID in same run (primary key constraint)."""
    with get_session() as session:
        # Create first issue
        issue1 = ReportedIssue(agent_run_id=test_critic_run, issue_id="duplicate-id", rationale="First version")
        session.add(issue1)
        session.commit()

        # Try to create second issue with same ID (should fail)
        issue2 = ReportedIssue(
            agent_run_id=test_critic_run,
            issue_id="duplicate-id",  # Duplicate
            rationale="Second version",
        )
        session.add(issue2)

        with pytest.raises(IntegrityError):
            session.commit()

        # Rollback to clean up the failed transaction
        session.rollback()


def test_foreign_key_cascade_delete(test_critic_run):
    """Deleting reported_issue cascades to occurrences."""
    with get_session() as session:
        # Create issue with occurrence (use real fixture file)
        issue = ReportedIssue(agent_run_id=test_critic_run, issue_id="cascade-test", rationale="Test issue")
        session.add(issue)
        session.flush()

        occ = ReportedIssueOccurrence(
            agent_run_id=test_critic_run, reported_issue_id="cascade-test", locations=[DBLocationAnchor(file="add.py")]
        )
        session.add(occ)
        session.commit()

        occ_id = occ.id

    # Delete issue (should cascade to occurrence)
    with get_session() as session:
        issue = session.query(ReportedIssue).filter_by(agent_run_id=test_critic_run, issue_id="cascade-test").one()
        session.delete(issue)
        session.commit()

        # Verify occurrence was deleted
        occ_count = session.query(ReportedIssueOccurrence).filter_by(id=occ_id).count()
        assert occ_count == 0, "Occurrence should be deleted via cascade"


if __name__ == "__main__":
    pytest_bazel.main()
