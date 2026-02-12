"""E2E test for grader sleep-wake cycle.

Tests that the snapshot grader:
1. Grades initial drift, then sleeps
2. Wakes on pg_notify when new critic data arrives
3. Grades the new data in the same agent loop (context retained)

Test flow:
- Insert critic-1 (one issue) BEFORE starting grader
- Grader grades issue with insert_edges (credit=0.1), sleeps
- While sleeping, insert critic-2 (one issue) — triggers pg_notify
- Grader wakes, grades second issue with insert_edges (credit=0.2)
- Await explicit round-complete events, then verify edges
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
from props.agents.grader.tools import EdgeSpec, FPRef, TPRef
from props.db.database import Database
from props.db.models import AgentRunStatus, GradingEdge, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import LocationAnchor
from props.testing.assertions import assert_no_pending
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import make_fake_critic_run

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


@pytest.mark.timeout(240)
async def test_grader_sleep_wake_cycle(e2e_stack, test_snapshot, all_files_scope, grader_image, db: Database):
    """Test that snapshot grader sleeps after grading, wakes on new drift, grades again.

    Train1 fixture for subtract.py produces 5 edges per issue:
    - 4 TP edges: tp-001/occ-1, tp-003/occ-1, tp-004/occ-1, tp-005/occ-1
    - 1 FP edge: fp-001/fp-occ-1

    Since we give TP edges credit > 0, issues won't appear in clustering_pending.
    """

    round_1_complete = asyncio.Event()
    round_2_complete = asyncio.Event()

    @GraderMock.mock(check_consumed=False)
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request (system prompt)

        # === Round 1: grade issue-1 ===
        # Train1 fixture for subtract.py: 4 TPs + 1 FP
        drift_1 = yield from m.get_drift_roundtrip()
        run_1 = drift_1.grading[0].critique_run_id

        yield from m.insert_edges_roundtrip(
            run_1,
            "issue-1",
            [
                EdgeSpec(gt_ref=TPRef(tp_id="tp-001", occurrence_id="occ-1"), credit=0.1),
                EdgeSpec(gt_ref=TPRef(tp_id="tp-003", occurrence_id="occ-1"), credit=0.1),
                EdgeSpec(gt_ref=TPRef(tp_id="tp-004", occurrence_id="occ-1"), credit=0.1),
                EdgeSpec(gt_ref=TPRef(tp_id="tp-005", occurrence_id="occ-1"), credit=0.1),
                EdgeSpec(gt_ref=FPRef(fp_id="fp-001", occurrence_id="fp-occ-1"), credit=0.0),
            ],
            "All edges for issue-1",
        )

        # No clustering expected (issue has credit > 0)
        drift_1_post = yield from m.get_drift_roundtrip()
        assert not drift_1_post.has_pending, f"Expected no pending after round 1: {drift_1_post!r}"

        round_1_complete.set()
        yield m.sleep("Round 1 complete")

        # === Round 2: grade issue-2 ===
        drift_2 = yield from m.get_drift_roundtrip()
        run_2 = drift_2.grading[0].critique_run_id

        yield from m.insert_edges_roundtrip(
            run_2,
            "issue-2",
            [
                EdgeSpec(gt_ref=TPRef(tp_id="tp-001", occurrence_id="occ-1"), credit=0.2),
                EdgeSpec(gt_ref=TPRef(tp_id="tp-003", occurrence_id="occ-1"), credit=0.2),
                EdgeSpec(gt_ref=TPRef(tp_id="tp-004", occurrence_id="occ-1"), credit=0.2),
                EdgeSpec(gt_ref=TPRef(tp_id="tp-005", occurrence_id="occ-1"), credit=0.2),
                EdgeSpec(gt_ref=FPRef(fp_id="fp-001", occurrence_id="fp-occ-1"), credit=0.0),
            ],
            "All edges for issue-2",
        )

        round_2_complete.set()
        yield from m.sleep_forever("Round 2 complete")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
        # --- Insert critic-1 BEFORE starting grader ---
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
                    locations=[LocationAnchor(file="subtract.py", start_line=1, end_line=1)],
                )
            )
            session.commit()

        assert get_drift(test_snapshot, db).grading, "grading_pending should have rows"

        # --- Start snapshot grader ---
        grader_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(
                image=stack.resolved_images["grader"], snapshot_slug=test_snapshot, model=stack.model
            ),
            name="snapshot-grader",
        )

        # --- Wait for round 1 to complete via explicit signal ---
        try:
            await asyncio.wait_for(round_1_complete.wait(), timeout=90)
        except TimeoutError:
            if grader_task.done():
                exc = grader_task.exception()
                if exc:
                    raise RuntimeError(f"Snapshot grader failed: {exc}") from exc
            raise AssertionError("Round 1 did not complete within timeout")

        # Verify round 1 TP edges
        with db.session() as session:
            tp_edge_1 = (
                session.query(GradingEdge)
                .filter_by(critique_run_id=critic_1_id, critique_issue_id="issue-1")
                .filter(GradingEdge.credit > 0)
                .first()
            )
            assert tp_edge_1 is not None, "No TP edge with credit>0 for round 1"
            assert tp_edge_1.credit == pytest.approx(0.1)
            logger.info(f"Round 1 TP edge verified: credit={tp_edge_1.credit}")

        # --- Insert critic-2 while grader is sleeping (triggers pg_notify) ---
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
                    locations=[LocationAnchor(file="subtract.py", start_line=1, end_line=1)],
                )
            )
            session.commit()

        logger.info("Critic-2 inserted, waiting for grader to wake and grade")

        # --- Wait for round 2 to complete via explicit signal ---
        try:
            await asyncio.wait_for(round_2_complete.wait(), timeout=90)
        except TimeoutError:
            if grader_task.done():
                exc = grader_task.exception()
                if exc:
                    raise RuntimeError(f"Snapshot grader failed: {exc}") from exc
            raise AssertionError("Round 2 did not complete within timeout")

        # --- Cleanup ---
        grader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await grader_task

        # Verify both edges exist with correct credits
        with db.session() as session:
            tp_edge_1 = (
                session.query(GradingEdge)
                .filter_by(critique_run_id=critic_1_id, critique_issue_id="issue-1")
                .filter(GradingEdge.credit > 0)
                .first()
            )
            tp_edge_2 = (
                session.query(GradingEdge)
                .filter_by(critique_run_id=critic_2_id, critique_issue_id="issue-2")
                .filter(GradingEdge.credit > 0)
                .first()
            )
            assert tp_edge_1 is not None
            assert tp_edge_2 is not None
            assert tp_edge_1.credit == pytest.approx(0.1)
            assert tp_edge_2.credit == pytest.approx(0.2)
            logger.info("Both edges verified: round 1 credit=0.1, round 2 credit=0.2")

        # Assert no drift remains (grading + clustering)
        assert_no_pending(test_snapshot, db)


if __name__ == "__main__":
    pytest_bazel.main()
