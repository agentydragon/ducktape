"""E2E test for snapshot grader.

Tests that the snapshot grader:
1. Detects pending drift in grading_pending view
2. Picks up new critique issues and grades them
3. Creates GradingEdge records
4. Clusters unmatched issues (credit=0)

Test flow:
- Insert drift data (completed critic run with reported issues) BEFORE starting grader
- Start snapshot grader container (runs indefinitely)
- Grader finds drift on first check, grades the issues, creates GradingEdge
- Grader clusters unmatched issues, then sleeps
- Assert no drift remains (grading + clustering)
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import pytest
import pytest_bazel

from agent_core.testing.responses import PlayGen
from props.agents.grader.drift_handler import get_drift
from props.agents.grader.testing.mocks import GraderMock
from props.agents.grader.tools import ClusterMemberSpec
from props.db.database import Database
from props.db.models import AgentRunStatus, GradingEdge, IssueCluster, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import LocationAnchor
from props.testing.assertions import assert_no_pending
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import make_fake_critic_run

logger = logging.getLogger(__name__)


@pytest.mark.timeout(180)
async def test_grader_picks_up_drift(e2e_stack, test_snapshot, all_files_scope, grader_image, db: Database):
    """Test that snapshot grader detects, grades, and clusters new critique issues.

    Train1 fixture for subtract.py produces 6 edges:
    - 5 TP edges: tp-001/occ-1, tp-003/occ-1, tp-004/occ-1, tp-005/occ-1, tp-006/occ-subtract
    - 1 FP edge: fp-001/fp-occ-1

    All graded with credit=0, so issue appears in clustering_pending.
    """

    grading_done = asyncio.Event()

    @GraderMock.mock(check_consumed=False)  # Grader may be aborted before consuming all
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        # Get pending edges for test-issue-1 on subtract.py
        drift = yield from m.get_drift_roundtrip()
        run_id = drift.grading[0].critique_run_id

        # Fill all 6 edges with credit=0
        yield from m.fill_remaining_roundtrip(run_id, "test-issue-1", 6, "No matching ground truth")

        # Issue has credit=0, so it appears in clustering_pending
        drift = yield from m.get_drift_roundtrip()
        assert len(drift.clustering) == 1, f"Expected 1 clustering issue, got {len(drift.clustering)}"

        # Cluster the single issue
        yield from m.create_cluster_roundtrip(
            "novel-issues",
            "Novel issues not in ground truth",
            [ClusterMemberSpec(run=run_id, issue_id="test-issue-1", rationale="Novel issue")],
        )

        grading_done.set()
        yield from m.sleep_forever("Graded and clustered all pending")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
        # Create drift BEFORE starting grader so it finds drift on first check.
        critic_run_id = uuid4()
        with db.session() as session:
            critic_run = make_fake_critic_run(
                session=session,
                example=all_files_scope,
                model=stack.model,
                status=AgentRunStatus.EXITED,
                agent_run_id=critic_run_id,
            )
            session.add(critic_run)
            session.flush()

            issue = ReportedIssue(
                agent_run_id=critic_run_id, issue_id="test-issue-1", rationale="Test issue for grader e2e"
            )
            session.add(issue)
            session.add(
                ReportedIssueOccurrence(
                    agent_run_id=critic_run_id,
                    reported_issue_id="test-issue-1",
                    locations=[LocationAnchor(file="subtract.py", start_line=1, end_line=1)],
                )
            )
            session.commit()

            logger.info(f"Created critic run {critic_run_id} with reported issue")

        # Precondition: verify grading_pending has rows before starting grader
        drift = get_drift(test_snapshot, db)
        assert drift.grading, "grading_pending should have rows but is empty"

        # Start snapshot grader
        grader_handle = await stack.registry.start_snapshot_grader(
            image=stack.resolved_images["grader"], snapshot_slug=test_snapshot, model=stack.model
        )

        # Wait for grading + clustering to complete
        async with grader_handle:
            try:
                await asyncio.wait_for(grading_done.wait(), timeout=90)
            except TimeoutError:
                raise AssertionError("Grading did not complete within timeout")

        # Assert grading happened
        with db.session() as session:
            grading_edge = (
                session.query(GradingEdge)
                .filter_by(critique_run_id=critic_run_id, critique_issue_id="test-issue-1")
                .first()
            )
            assert grading_edge is not None, "GradingEdge was not created"
            assert grading_edge.credit == 0.0
            assert grading_edge.rationale is not None
            assert "No matching ground truth" in grading_edge.rationale

        # Assert clustering happened
        with db.session() as session:
            cluster = session.query(IssueCluster).filter_by(snapshot_slug=test_snapshot).first()
            assert cluster is not None, "Issue cluster was not created"

        # Assert no drift remains (grading + clustering)
        assert_no_pending(test_snapshot, db)


if __name__ == "__main__":
    pytest_bazel.main()
