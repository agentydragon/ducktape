"""E2E test for grader daemon.

Tests that the grader daemon:
1. Starts and listens for pg_notify on grading_pending
2. Picks up new critique issues and grades them
3. Creates GradingEdge records

Test flow:
- Start grader daemon container (runs indefinitely)
- Create a completed critic run with reported issues
- pg_notify fires automatically via database triggers
- Daemon wakes, grades the issues, creates GradingEdge
- Poll for GradingEdge creation, then cancel daemon
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import uuid4

import pytest
import pytest_bazel

from agent_core_testing.responses import PlayGen
from props.db.models import AgentRunStatus, GradingEdge, ReportedIssue, ReportedIssueOccurrence
from props.db.session import get_session
from props.db.snapshots import DBLocationAnchor
from props.testing.fixtures.runs import make_critic_run
from props.testing.mocks import GraderMock

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.requires_docker]

TEST_TIMEOUT_SECONDS = 60


def make_grader_daemon_mock() -> GraderMock:
    """Create mock for grader daemon that grades one pending issue.

    Note: Grader daemons are eternal - they don't submit/exit. The daemon
    will keep running after grading, sleeping until more drift appears.
    The test cancels the daemon task after verifying grading completed.
    """

    @GraderMock.mock()
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        # List pending items
        pending = yield from m.list_pending_roundtrip()
        logger.info(f"Grader found {len(pending)} pending items")

        # Grade each pending item
        for edge in pending:
            logger.info(f"Grading issue {edge.critique_issue_id} from run {edge.critique_run_id}")

            # Mark as no match (FP with 0 credit)
            yield from m.fill_remaining_roundtrip(
                edge.critique_run_id, edge.critique_issue_id, 0, "No matching ground truth"
            )

        # Daemon continues running (eternal) - test will cancel after verifying grading

    return mock


@pytest.mark.timeout(120)
async def test_grader_daemon_picks_up_drift(e2e_stack, test_snapshot, all_files_scope):
    """Test that grader daemon detects and grades new critique issues."""
    mock = make_grader_daemon_mock()

    async with e2e_stack(mock) as stack:
        # Start grader daemon in background task
        daemon_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(snapshot_slug=test_snapshot, model=stack.model), name="grader-daemon"
        )

        # Give daemon time to start and begin listening
        await asyncio.sleep(2)

        # Create drift: insert a completed critic run with reported issues
        critic_run_id = uuid4()
        with get_session() as session:
            # Create critic run
            critic_run = make_critic_run(
                example=all_files_scope, model=stack.model, status=AgentRunStatus.COMPLETED, agent_run_id=critic_run_id
            )
            session.add(critic_run)
            session.flush()

            # Add a reported issue
            issue = ReportedIssue(
                agent_run_id=critic_run_id, issue_id="test-issue-1", rationale="Test issue for grader daemon e2e"
            )
            session.add(issue)

            # Add occurrence (required for grading_pending to pick it up)
            occurrence = ReportedIssueOccurrence(
                agent_run_id=critic_run_id,
                reported_issue_id="test-issue-1",
                locations=[DBLocationAnchor(file="subtract.py", start_line=1, end_line=1)],
            )
            session.add(occurrence)
            session.commit()

            logger.info(f"Created critic run {critic_run_id} with reported issue")

        # Poll for GradingEdge creation
        grading_edge = None
        for _ in range(30):  # Poll for up to 30 seconds
            await asyncio.sleep(1)

            with get_session() as session:
                grading_edge = (
                    session.query(GradingEdge)
                    .filter_by(critique_run_id=critic_run_id, critique_issue_id="test-issue-1")
                    .first()
                )
                if grading_edge:
                    logger.info(f"GradingEdge created: {grading_edge}")
                    break

        # Cancel daemon
        daemon_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon_task

        # Assert grading happened
        assert grading_edge is not None, "GradingEdge was not created within timeout"
        assert grading_edge.credit == 0.0  # We mocked fill_remaining with 0 credit
        assert "No matching ground truth" in grading_edge.rationale


if __name__ == "__main__":
    pytest_bazel.main()
