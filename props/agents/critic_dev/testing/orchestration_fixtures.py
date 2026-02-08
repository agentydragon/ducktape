"""Shared constants and mocks for multi-model orchestration e2e tests."""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

from agent_core.testing.responses import PlayGen
from props.agents.grader.testing.mocks import GraderMock

logger = logging.getLogger(__name__)

# Model names for multi-model routing.
# Must exist in synced model_metadata (llm_requests.model has FK to model_metadata.model_id).
# Each must be DISTINCT so multi-model FakeOpenAIServer routes to the right mock.
ORCHESTRATION_OPTIMIZER_MODEL = "gpt-4o"
ORCHESTRATION_CRITIC_MODEL = "gpt-4o-mini"
ORCHESTRATION_GRADER_MODEL = "gpt-4.1-mini"


def make_orchestration_grader_mock() -> GraderMock:
    """Create grader mock that fills all pending edges with credit=0.

    Single-shot: the daemon's initial-drift-wait guarantees pending edges
    exist by the time the agent loop starts, so one round suffices.
    """

    @GraderMock.mock(check_consumed=False)  # Daemon may be cancelled before consuming all
    def mock(m: GraderMock) -> PlayGen:
        yield None  # First request (system message)

        pending = yield from m.list_pending_roundtrip()
        logger.info(f"Grader mock: got {len(pending)} pending edges")

        # Group by (run, issue_id) to batch fill_remaining calls
        by_issue: dict[tuple[UUID, str], int] = defaultdict(int)
        for edge in pending:
            key = (edge.critique_run_id, edge.critique_issue_id)
            by_issue[key] += 1

        for (run_id, issue_id), count in by_issue.items():
            logger.info(f"Grader mock: filling {count} edges for {run_id}/{issue_id}")
            yield from m.fill_remaining_roundtrip(run_id, issue_id, count, "Mock: no GT matches")

        yield m.sleep("All edges graded")

    return mock
