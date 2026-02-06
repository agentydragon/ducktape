"""E2E test for grader daemon.

Tests that the grader daemon:
1. Detects pending drift in grading_pending view
2. Picks up new critique issues and grades them
3. Creates GradingEdge records

Test flow:
- Insert drift data (completed critic run with reported issues) BEFORE starting daemon
- Start grader daemon container (runs indefinitely)
- Daemon finds drift on first check, grades the issues, creates GradingEdge
- Poll for GradingEdge creation, then cancel daemon
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import text

from agent_core.testing.responses import PlayGen
from props.db.database import Database
from props.db.models import AgentRunStatus, GradingEdge, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import DBLocationAnchor
from props.grader.testing.mocks import GraderMock
from props.testing.fixtures.runs import make_fake_critic_run

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.requires_docker]


@pytest.mark.timeout(180)
async def test_grader_daemon_picks_up_drift(e2e_stack, test_snapshot, all_files_scope, grader_image, db: Database):
    """Test that grader daemon detects and grades new critique issues."""

    @GraderMock.mock(check_consumed=False)  # Daemon may be aborted before consuming all
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        # List pending items
        pending = yield from m.list_pending_roundtrip()
        logger.info(f"Grader found {len(pending)} pending items")

        # Group by (run_id, issue_id) and fill each group at once
        by_issue: dict[tuple[UUID, str], int] = defaultdict(int)
        for edge in pending:
            by_issue[(edge.critique_run_id, edge.critique_issue_id)] += 1

        for (run_id, issue_id), count in by_issue.items():
            logger.info(f"Grading {count} edges for issue {issue_id} from run {run_id}")
            yield from m.fill_remaining_roundtrip(run_id, issue_id, count, "No matching ground truth")

        # Signal that grading is done — sleep tool checks grading_pending is empty
        yield m.sleep("Graded all pending edges")

    async with e2e_stack(mock, images=[grader_image]) as stack:
        # Create drift BEFORE starting daemon so it finds drift on first check.
        # This avoids relying on pg_notify timing (which can be unreliable in Docker).
        critic_run_id = uuid4()
        with db.session() as session:
            critic_run = make_fake_critic_run(
                session=session,
                example=all_files_scope,
                model=stack.model,
                status=AgentRunStatus.COMPLETED,
                agent_run_id=critic_run_id,
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

        # Precondition: verify grading_pending has rows before starting daemon
        with db.session() as session:
            pending_count = session.execute(
                text("SELECT count(*) FROM grading_pending WHERE snapshot_slug = :slug"), {"slug": test_snapshot}
            ).scalar()
            assert pending_count is not None, "grading_pending query returned None"
            assert int(pending_count) > 0, f"grading_pending should have rows but has {pending_count}"

        # Start grader daemon in background task — drift already exists in DB
        daemon_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(snapshot_slug=test_snapshot, model=stack.model), name="grader-daemon"
        )

        # Poll for GradingEdge creation
        edge_credit: float | None = None
        edge_rationale: str | None = None
        found = False
        for _ in range(90):
            await asyncio.sleep(1)

            # Check if daemon task failed (surface errors early)
            if daemon_task.done() and not daemon_task.cancelled():
                exc = daemon_task.exception()
                if exc:
                    raise RuntimeError(f"Grader daemon failed: {exc}") from exc

            with db.session() as session:
                grading_edge = (
                    session.query(GradingEdge)
                    .filter_by(critique_run_id=critic_run_id, critique_issue_id="test-issue-1")
                    .first()
                )
                if grading_edge:
                    edge_credit = grading_edge.credit
                    edge_rationale = grading_edge.rationale
                    found = True
                    logger.info(f"GradingEdge created: credit={edge_credit}, rationale={edge_rationale}")
                    break

        # Cancel daemon
        daemon_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon_task

        # Assert grading happened
        assert found, "GradingEdge was not created within timeout"
        assert edge_credit == 0.0  # We mocked fill_remaining with 0 credit
        assert edge_rationale is not None
        assert "No matching ground truth" in edge_rationale


if __name__ == "__main__":
    pytest_bazel.main()
