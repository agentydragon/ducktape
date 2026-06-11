"""Test SQL constraints on grading_edges table.

Verifies that database constraints correctly enforce:
- Exactly ONE target type (TP / FP) via NULL pattern
- Credit must be between 0.0 and 1.0
- Credit sum ≤1.0 per occurrence (enforced by SQL trigger)
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from props.core.db.models import AgentRunStatus, GradingEdge
from props.core.db.session import get_session
from props.testing.fixtures import make_critic_run, make_grader_run, make_reported_issues

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def session(synced_test_db):
    """Provide a database session for the test."""
    with get_session() as sess:
        yield sess


@pytest.fixture
def test_critic_run(session, example_subtract_orm):
    """Create a completed critic run for testing."""
    critic_run = make_critic_run(example=example_subtract_orm, status=AgentRunStatus.COMPLETED)
    session.add(critic_run)
    session.commit()
    return critic_run


@pytest.fixture
def test_grader_run(session, test_critic_run):
    """Create a grader run for testing edges."""
    grader_run = make_grader_run(critic_run=test_critic_run, status=AgentRunStatus.IN_PROGRESS)
    session.add(grader_run)
    session.commit()
    return grader_run


@pytest.fixture
def add_edge(session, test_grader_run, test_critic_run, example_subtract_orm):
    """Fixture factory for creating grading edges.

    Always creates a fresh ReportedIssue then the edge. Caller must ensure issue_id uniqueness.
    """

    def _add(critique_issue_id: str, rationale: str = "Test edge", **kwargs):
        # Always create the ReportedIssue - caller must ensure unique issue_ids
        make_reported_issues(agent_run_id=test_critic_run.agent_run_id, issue_ids=[critique_issue_id], session=session)

        edge = GradingEdge(
            grader_run_id=test_grader_run.agent_run_id,
            critique_run_id=test_critic_run.agent_run_id,
            critique_issue_id=critique_issue_id,
            snapshot_slug=example_subtract_orm.snapshot_slug,
            rationale=rationale,
            **kwargs,
        )
        session.add(edge)
        return edge

    return _add


def test_edge_tp_match_valid(session, add_edge, tp_occurrence_single):
    """Valid: TP match with both tp_id and tp_occurrence_id."""
    tp_id, occ_id = tp_occurrence_single
    add_edge("issue-001", tp_id=tp_id, tp_occurrence_id=occ_id, credit=0.8, rationale="Matches TP")
    session.commit()


def test_edge_fp_match_valid(session, add_edge, fp_occurrence):
    """Valid: FP match with both fp_id and fp_occurrence_id."""
    fp_id, fp_occ = fp_occurrence
    add_edge("issue-002", fp_id=fp_id, fp_occurrence_id=fp_occ, credit=1.0, rationale="Matches FP")
    session.commit()


def test_edge_partial_tp_target_invalid(session, add_edge, tp_occurrence_single):
    """Invalid: TP match requires BOTH tp_id and tp_occurrence_id."""
    tp_id, _ = tp_occurrence_single
    add_edge("issue-001", tp_id=tp_id, credit=0.5, rationale="Invalid: incomplete TP target")

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_edge_partial_fp_target_invalid(session, add_edge, fp_occurrence):
    """Invalid: FP match requires BOTH fp_id and fp_occurrence_id."""
    fp_id, _ = fp_occurrence
    add_edge("issue-002", fp_id=fp_id, credit=0.5, rationale="Invalid: incomplete FP target")

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_edge_credit_range_valid(session, add_edge, tp_occurrences_multi):
    """Valid: credit can be any value between 0.0 and 1.0.

    Uses cross-cutting TPs (tp-003, tp-004, tp-005 which have NULL graders_match_only_if_reported_on)
    to avoid file scope conflicts. Each edge goes to a different occurrence to
    avoid the credit sum ≤ 1.0 constraint.
    """
    # Get cross-cutting occurrences (tp-003+, which don't have graders_match_only_if_reported_on)
    # tp-001 and tp-002 have graders_match_only_if_reported_on set, so skip them
    cross_cutting = [(tp, occ) for tp, occ in tp_occurrences_multi if tp >= "tp-003"]
    assert len(cross_cutting) >= 3, f"Need at least 3 cross-cutting TPs, got {len(cross_cutting)}"

    # Create edges with different credit values, each to different occurrences
    test_cases = [
        (0.0, "issue-001", cross_cutting[0][0], cross_cutting[0][1]),
        (0.5, "issue-002", cross_cutting[1][0], cross_cutting[1][1]),
        (1.0, "issue-003", cross_cutting[2][0], cross_cutting[2][1]),
    ]
    for credit, issue_id, tp_id, occ_id in test_cases:
        add_edge(issue_id, tp_id=tp_id, tp_occurrence_id=occ_id, credit=credit, rationale=f"Valid credit: {credit}")
    session.commit()


def test_edge_credit_negative_invalid(session, add_edge, tp_occurrence_single):
    """Invalid: credit cannot be negative."""
    tp_id, occ_id = tp_occurrence_single
    add_edge("issue-001", tp_id=tp_id, tp_occurrence_id=occ_id, credit=-0.5, rationale="Invalid: negative credit")

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_edge_credit_above_one_invalid(session, add_edge, tp_occurrence_single):
    """Invalid: credit cannot exceed 1.0.

    Note: The credit_sum trigger catches this before the CHECK constraint,
    so we get a RaiseException (via psycopg2.errors) rather than IntegrityError.
    """
    tp_id, occ_id = tp_occurrence_single
    add_edge("issue-001", tp_id=tp_id, tp_occurrence_id=occ_id, credit=1.5, rationale="Invalid: credit > 1.0")

    with pytest.raises(Exception, match=r"Credit sum .* would exceed 1\.0"):
        session.commit()
    session.rollback()


def test_credit_sum_trigger_enforces_limit_tp(session, add_edge, tp_occurrence_single):
    """SQL trigger enforces credit sum ≤1.0 per TP occurrence."""
    tp_id, occ_id = tp_occurrence_single
    add_edge("issue-001", tp_id=tp_id, tp_occurrence_id=occ_id, credit=0.7, rationale="First match")
    session.commit()

    add_edge("issue-002", tp_id=tp_id, tp_occurrence_id=occ_id, credit=0.5, rationale="Second match")

    with pytest.raises(Exception, match=r"Credit sum .* would exceed 1\.0"):
        session.commit()
    session.rollback()


def test_credit_sum_trigger_allows_exactly_one(session, add_edge, tp_occurrence_single):
    """SQL trigger allows credit sum = 1.0 (boundary case)."""
    tp_id, occ_id = tp_occurrence_single
    add_edge("issue-001", tp_id=tp_id, tp_occurrence_id=occ_id, credit=0.6, rationale="First match")
    session.commit()

    add_edge("issue-002", tp_id=tp_id, tp_occurrence_id=occ_id, credit=0.4, rationale="Second match")
    session.commit()


def test_credit_sum_trigger_enforces_limit_fp(session, add_edge, fp_occurrence):
    """SQL trigger enforces credit sum ≤1.0 per FP occurrence."""
    fp_id, fp_occ = fp_occurrence
    add_edge("issue-001", fp_id=fp_id, fp_occurrence_id=fp_occ, credit=0.8, rationale="First FP match")
    session.commit()

    add_edge("issue-002", fp_id=fp_id, fp_occurrence_id=fp_occ, credit=0.3, rationale="Second FP match")

    with pytest.raises(Exception, match=r"Credit sum .* would exceed 1\.0"):
        session.commit()
    session.rollback()
