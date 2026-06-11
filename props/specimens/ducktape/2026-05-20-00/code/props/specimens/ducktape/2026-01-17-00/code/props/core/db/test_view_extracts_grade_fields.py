"""Test that the occurrence_credits view correctly extracts fields from GraderOutput."""

from uuid import uuid4

from sqlalchemy import text

from props.core.db.config import DatabaseConfig
from props.core.db.examples import Example
from props.core.db.models import AgentRunStatus, GradingEdge, TruePositive
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug
from props.testing.fixtures import (
    EMPTY_CANONICAL_ISSUES_SNAPSHOT,
    make_critic_run,
    make_grader_run,
    make_reported_issues,
)


def test_view_extracts_grade_fields_correctly(synced_test_db: DatabaseConfig):
    """Test that the view includes grader runs with occurrence-based results.

    Uses git-synced test fixtures (test-fixtures/train1) instead of synthetic data.
    The test-trivial fixture has a TP 'test-issue' with occurrence 'occ-1' in subtract.py.
    """
    # Use git-synced fixture: test-fixtures/train1 (TRAIN split)
    snapshot_slug = SnapshotSlug("test-fixtures/train1")
    critic_agent_run_id = uuid4()
    grader_agent_run_id = uuid4()

    with get_session() as session:
        # Get an existing example from git fixtures - use any single-file-set example
        example = (
            session.query(Example).filter_by(snapshot_slug=snapshot_slug).filter(Example.files_hash.isnot(None)).first()
        )
        assert example is not None, "Expected a single-file-set example in test-trivial"

        # Get the actual TP from the fixture to match against
        tps = session.query(TruePositive).filter_by(snapshot_slug=snapshot_slug).all()
        # The test-trivial fixture has test-issue with occ-1 in subtract.py
        # Find the TP that has subtract.py in its occurrences
        matching_tp = None
        matching_occ_id = None
        for tp in tps:
            for occ in tp.occurrences:
                if "subtract.py" in [str(r.file_path) for r in occ.ranges]:
                    matching_tp = tp
                    matching_occ_id = occ.occurrence_id
                    break
            if matching_tp:
                break

        assert matching_tp is not None, "Should find a TP with subtract.py"
        assert matching_occ_id is not None

        # Insert critic run (required for view join) using fixture factory
        critic_run = make_critic_run(example=example, agent_run_id=critic_agent_run_id, status=AgentRunStatus.COMPLETED)
        session.add(critic_run)
        session.flush()

        # Create reported issues first (required for grading decisions FK)
        issue_ids = ["input-test-001"]
        make_reported_issues(agent_run_id=critic_run.agent_run_id, issue_ids=issue_ids, session=session)

        # Insert grader run with output using fixture factory
        grader_run = make_grader_run(
            critic_run=critic_run,
            canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT,
            model="test-grader-model",
            agent_run_id=grader_agent_run_id,
        )
        session.add(grader_run)
        session.flush()

        # Create grading edge matching the git fixture TP
        edge = GradingEdge(
            critique_run_id=critic_run.agent_run_id,
            critique_issue_id="input-test-001",
            snapshot_slug=snapshot_slug,
            tp_id=matching_tp.tp_id,
            tp_occurrence_id=matching_occ_id,
            fp_id=None,
            fp_occurrence_id=None,
            credit=1.0,
            rationale="Fully found this occurrence",
            grader_run_id=grader_run.agent_run_id,
        )
        session.add(edge)

        session.commit()

        # Query the occurrence_credits view - verify the run appears with occurrence results
        result = session.execute(
            text("""
                SELECT grader_run_id, tp_id, occurrence_id, found_credit
                FROM occurrence_credits
                WHERE snapshot_slug = :slug
            """),
            {"slug": str(snapshot_slug)},
        ).fetchone()

        assert result is not None, "View should return a row for the grader run"
        assert result.grader_run_id == grader_run.agent_run_id, "Should match the grader run ID"
        assert result.tp_id == matching_tp.tp_id, "Should extract tp_id from grading_edges"
        assert result.occurrence_id == matching_occ_id, "Should extract occurrence_id from grading_edges"
        assert result.found_credit == 1.0, "Should extract credit from grading_edges"
