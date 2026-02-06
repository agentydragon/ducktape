"""Shared constants and mocks for multi-model orchestration e2e tests."""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

from agent_core.testing.responses import PlayGen
from props.testing.mocks import GraderMock

logger = logging.getLogger(__name__)

# Model names for multi-model routing
ORCHESTRATION_OPTIMIZER_MODEL = "test-orchestration-optimizer"
ORCHESTRATION_CRITIC_MODEL = "test-orchestration-critic"
ORCHESTRATION_GRADER_MODEL = "test-orchestration-grader"


def make_orchestration_grader_mock() -> GraderMock:
    """Create grader mock that fills all pending edges with credit=0.

    The grader daemon runs in DAEMON mode which has no submit tool.
    It processes edges until the drift handler sees no pending drift and aborts.
    """

    @GraderMock.mock(check_consumed=False)  # Daemon may be aborted before consuming all
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request (system message)

        # Get all pending edges
        pending = yield from m.list_pending_roundtrip()
        logger.info(f"Grader mock: got {len(pending)} pending edges")

        if not pending:
            # No pending edges - shouldn't happen but handle gracefully
            logger.warning("Grader mock: no pending edges to process")
            return

        # Group by (run, issue_id) to batch fill_remaining calls
        by_issue: dict[tuple[UUID, str], int] = defaultdict(int)
        for edge in pending:
            key = (edge.critique_run_id, edge.critique_issue_id)
            by_issue[key] += 1

        # Fill each issue's remaining edges
        for (run_id, issue_id), count in by_issue.items():
            logger.info(f"Grader mock: filling {count} edges for {run_id}/{issue_id}")
            yield from m.fill_remaining_roundtrip(run_id, issue_id, count, "Mock: no GT matches")

        # After filling, the drift handler will see no drift and abort the loop
        logger.info("Grader mock: all edges filled, drift handler should abort")

    return mock
