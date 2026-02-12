"""E2E test for grader issue clustering.

Tests that the snapshot grader clusters 2 novel issues from 2 different critic
runs into a single cluster:
1. Both issues get graded with credit=0 (no GT match)
2. Both appear in clustering_pending
3. Grader creates a cluster grouping them
4. No drift remains after sleep
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import uuid4

import pytest
import pytest_bazel

from agent_core.testing.responses import PlayGen
from props.agents.grader.drift_handler import get_drift
from props.agents.grader.testing.mocks import GraderMock
from props.agents.grader.tools import ClusterMemberSpec
from props.db.database import Database
from props.db.models import AgentRunStatus, IssueCluster, IssueClusterMember, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import LocationAnchor
from props.testing.assertions import assert_no_pending
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import make_fake_critic_run

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


@pytest.mark.timeout(180)
async def test_grader_clusters_novel_issues_from_two_critiques(
    e2e_stack, test_snapshot, all_files_scope, grader_image, db: Database
):
    """Test that grader clusters 2 unmatched issues from 2 different critic runs.

    Train1 fixture for parser.py produces 4 edges per issue:
    - 3 TP edges: tp-003/occ-1, tp-004/occ-1, tp-005/occ-1 (no match_file_restriction)
    - 1 FP edge: fp-001/fp-occ-1 (no match_file_restriction)

    2 issues x 4 edges = 8 total edges, all graded with credit=0.
    Both issues appear in clustering_pending.
    """

    clustering_done = asyncio.Event()
    # Store run IDs for explicit mock operations
    critic_1_id = uuid4()
    critic_2_id = uuid4()

    @GraderMock.mock(check_consumed=False)
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        # Get pending edges (8 total: 4 per issue)
        drift = yield from m.get_drift_roundtrip()

        # Fill all edges for null-check-1 (4 edges)
        yield from m.fill_remaining_roundtrip(critic_1_id, "null-check-1", 4, "No matching ground truth")

        # Fill all edges for null-check-2 (4 edges)
        yield from m.fill_remaining_roundtrip(critic_2_id, "null-check-2", 4, "No matching ground truth")

        # Both issues have credit=0, so both appear in clustering_pending
        drift = yield from m.get_drift_roundtrip()
        assert len(drift.clustering) == 2, f"Expected 2 clustering issues, got {len(drift.clustering)}"

        # Cluster both issues together
        yield from m.create_cluster_roundtrip(
            "missing-null-check",
            "Both critics found the same missing null check",
            [
                ClusterMemberSpec(
                    run=critic_1_id, issue_id="null-check-1", rationale="Reports the same missing null check"
                ),
                ClusterMemberSpec(
                    run=critic_2_id, issue_id="null-check-2", rationale="Reports the same missing null check"
                ),
            ],
        )

        clustering_done.set()
        yield from m.sleep_forever("Graded and clustered all pending")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
        # Create 2 critic runs using the IDs defined above (for mock to reference)
        with db.session() as session:
            for cid, issue_name in [(critic_1_id, "null-check-1"), (critic_2_id, "null-check-2")]:
                critic_run = make_fake_critic_run(
                    session=session,
                    example=all_files_scope,
                    model=stack.model,
                    status=AgentRunStatus.EXITED,
                    agent_run_id=cid,
                )
                session.add(critic_run)
                session.flush()
                session.add(
                    ReportedIssue(agent_run_id=cid, issue_id=issue_name, rationale=f"Missing null check ({cid})")
                )
                session.add(
                    ReportedIssueOccurrence(
                        agent_run_id=cid,
                        reported_issue_id=issue_name,
                        locations=[LocationAnchor(file="parser.py", start_line=42, end_line=42)],
                    )
                )
            session.commit()

        logger.info(f"Created 2 critic runs: {critic_1_id}, {critic_2_id}")

        # Precondition: verify grading_pending has rows
        assert get_drift(test_snapshot, db).grading, "grading_pending should have rows"

        # Start snapshot grader
        grader_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(
                image=stack.resolved_images["grader"], snapshot_slug=test_snapshot, model=stack.model
            ),
            name="snapshot-grader",
        )

        # Wait for clustering to complete
        try:
            await asyncio.wait_for(clustering_done.wait(), timeout=90)
        except TimeoutError:
            if grader_task.done():
                exc = grader_task.exception()
                if exc:
                    raise RuntimeError(f"Snapshot grader failed: {exc}") from exc
            raise AssertionError("Clustering did not complete within timeout")

        # Cancel grader
        grader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await grader_task

        # Assert cluster was created with 2 members
        with db.session() as session:
            cluster = (
                session.query(IssueCluster)
                .filter_by(snapshot_slug=test_snapshot, cluster_id="missing-null-check")
                .first()
            )
            assert cluster is not None, "Cluster 'missing-null-check' was not created"
            assert cluster.rationale == "Both critics found the same missing null check"

            members = (
                session.query(IssueClusterMember)
                .filter_by(snapshot_slug=test_snapshot, cluster_id="missing-null-check")
                .all()
            )
            assert len(members) == 2, f"Expected 2 cluster members, got {len(members)}"

            member_issues = {(m.critique_run_id, m.critique_issue_id) for m in members}
            assert (critic_1_id, "null-check-1") in member_issues
            assert (critic_2_id, "null-check-2") in member_issues
            logger.info("Cluster verified: 2 members from 2 critic runs")

        # Assert no drift remains
        assert_no_pending(test_snapshot, db)


if __name__ == "__main__":
    pytest_bazel.main()
