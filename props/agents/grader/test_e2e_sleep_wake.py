"""E2E test for grader sleep-wake cycle.

Tests that the grader daemon:
1. Grades initial drift, then sleeps
2. Wakes on pg_notify when new critic data arrives
3. Grades the new data in the same agent loop (context retained)

Test flow:
- Insert critic-1 (one issue) BEFORE starting daemon
- Daemon grades issue with insert_edges (credit=0.1), sleeps
- While sleeping, insert critic-2 (one issue) — triggers pg_notify
- Daemon wakes, grades second issue with insert_edges (credit=0.2)
- Poll for both GradingEdge records, then cancel daemon
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from agent_core.testing.responses import PlayGen
from props.agents.grader.drift_handler import check_grading_pending
from props.agents.grader.testing.mocks import GraderMock
from props.agents.grader.tools import EdgeSpec, TPRef
from props.db.database import Database
from props.db.models import AgentRunStatus, GradingEdge, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import DBLocationAnchor
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import make_fake_critic_run

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


@pytest.mark.timeout(240)
async def test_grader_sleep_wake_cycle(e2e_stack, test_snapshot, all_files_scope, grader_image, db: Database):
    """Test that grader daemon sleeps after grading, wakes on new drift, grades again."""

    @GraderMock.mock(check_consumed=False)
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request (system prompt)

        # === Round 1: grade critic-1's issue with credit 0.1 ===
        pending_1 = yield from m.list_pending_roundtrip()
        logger.info(f"Round 1: {len(pending_1)} pending edges")

        by_issue_1: dict[tuple[UUID, str], list] = defaultdict(list)
        for edge in pending_1:
            by_issue_1[(edge.critique_run_id, edge.critique_issue_id)].append(edge)

        for (run_id, issue_id), edges in by_issue_1.items():
            tp_edges = [EdgeSpec(gt_ref=e.gt_ref, credit=0.1) for e in edges if isinstance(e.gt_ref, TPRef)]
            # fill_remaining for any non-TP edges (FP edges get credit=0)
            fp_count = len(edges) - len(tp_edges)
            if tp_edges:
                yield from m.insert_edges_roundtrip(run_id, issue_id, tp_edges, "Partial match round 1")
            if fp_count > 0:
                yield from m.fill_remaining_roundtrip(run_id, issue_id, fp_count, "No match round 1")

        # Sleep — sleep tool verifies grading_pending is empty, then awaits pg_notify
        yield m.sleep("Round 1 complete")

        # === Round 2: woken by pg_notify, grade critic-2's issue with credit 0.2 ===
        pending_2 = yield from m.list_pending_roundtrip()
        logger.info(f"Round 2: {len(pending_2)} pending edges")

        by_issue_2: dict[tuple[UUID, str], list] = defaultdict(list)
        for edge in pending_2:
            by_issue_2[(edge.critique_run_id, edge.critique_issue_id)].append(edge)

        for (run_id, issue_id), edges in by_issue_2.items():
            tp_edges = [EdgeSpec(gt_ref=e.gt_ref, credit=0.2) for e in edges if isinstance(e.gt_ref, TPRef)]
            fp_count = len(edges) - len(tp_edges)
            if tp_edges:
                yield from m.insert_edges_roundtrip(run_id, issue_id, tp_edges, "Partial match round 2")
            if fp_count > 0:
                yield from m.fill_remaining_roundtrip(run_id, issue_id, fp_count, "No match round 2")

        # Sleep again (will be cancelled by test)
        yield m.sleep("Round 2 complete")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
        # --- Insert critic-1 BEFORE starting daemon ---
        critic_1_id = uuid4()
        with db.session() as session:
            critic_1 = make_fake_critic_run(
                session=session,
                example=all_files_scope,
                model=stack.model,
                status=AgentRunStatus.EXITED,
                agent_run_id=critic_1_id,
            )
            session.add(critic_1)
            session.flush()
            session.add(ReportedIssue(agent_run_id=critic_1_id, issue_id="issue-1", rationale="Critic 1 issue"))
            session.add(
                ReportedIssueOccurrence(
                    agent_run_id=critic_1_id,
                    reported_issue_id="issue-1",
                    locations=[DBLocationAnchor(file="subtract.py", start_line=1, end_line=1)],
                )
            )
            session.commit()

        assert check_grading_pending(test_snapshot, db) > 0

        # --- Start daemon ---
        daemon_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(snapshot_slug=test_snapshot, model=stack.model), name="grader-daemon"
        )

        # --- Wait for round 1 edges to appear ---
        round_1_done = False
        for _ in range(90):
            await asyncio.sleep(1)
            if daemon_task.done() and not daemon_task.cancelled():
                exc = daemon_task.exception()
                if exc:
                    raise RuntimeError(f"Grader daemon failed: {exc}") from exc

            with db.session() as session:
                edge = (
                    session.query(GradingEdge)
                    .filter_by(critique_run_id=critic_1_id, critique_issue_id="issue-1")
                    .first()
                )
                if edge:
                    logger.info(f"Round 1 edge: credit={edge.credit}")
                    assert edge.credit == pytest.approx(0.1)
                    round_1_done = True
                    break

        assert round_1_done, "Round 1 GradingEdge not created within timeout"

        # Wait briefly for daemon to reach sleep (it needs to call sleep tool
        # and have the tool verify grading_pending is empty)
        for _ in range(30):
            await asyncio.sleep(1)
            if check_grading_pending(test_snapshot, db) == 0:
                break
        assert check_grading_pending(test_snapshot, db) == 0, "Daemon didn't finish grading round 1"

        # --- Insert critic-2 while daemon is sleeping (triggers pg_notify) ---
        critic_2_id = uuid4()
        with db.session() as session:
            critic_2 = make_fake_critic_run(
                session=session,
                example=all_files_scope,
                model=stack.model,
                status=AgentRunStatus.EXITED,
                agent_run_id=critic_2_id,
            )
            session.add(critic_2)
            session.flush()
            session.add(ReportedIssue(agent_run_id=critic_2_id, issue_id="issue-2", rationale="Critic 2 issue"))
            session.add(
                ReportedIssueOccurrence(
                    agent_run_id=critic_2_id,
                    reported_issue_id="issue-2",
                    locations=[DBLocationAnchor(file="subtract.py", start_line=1, end_line=1)],
                )
            )
            session.commit()

        logger.info("Critic-2 inserted, waiting for daemon to wake and grade")

        # --- Wait for round 2 edges to appear ---
        round_2_done = False
        for _ in range(90):
            await asyncio.sleep(1)
            if daemon_task.done() and not daemon_task.cancelled():
                exc = daemon_task.exception()
                if exc:
                    raise RuntimeError(f"Grader daemon failed: {exc}") from exc

            with db.session() as session:
                edge = (
                    session.query(GradingEdge)
                    .filter_by(critique_run_id=critic_2_id, critique_issue_id="issue-2")
                    .first()
                )
                if edge:
                    logger.info(f"Round 2 edge: credit={edge.credit}")
                    assert edge.credit == pytest.approx(0.2)
                    round_2_done = True
                    break

        # --- Cleanup ---
        daemon_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await daemon_task

        assert round_2_done, "Round 2 GradingEdge not created within timeout"

        # Verify both edges exist with correct credits
        with db.session() as session:
            edge_1 = (
                session.query(GradingEdge).filter_by(critique_run_id=critic_1_id, critique_issue_id="issue-1").first()
            )
            edge_2 = (
                session.query(GradingEdge).filter_by(critique_run_id=critic_2_id, critique_issue_id="issue-2").first()
            )
            assert edge_1 is not None
            assert edge_2 is not None
            assert edge_1.credit == pytest.approx(0.1)
            assert edge_2.credit == pytest.approx(0.2)
            logger.info("Both edges verified: round 1 credit=0.1, round 2 credit=0.2")


if __name__ == "__main__":
    pytest_bazel.main()
