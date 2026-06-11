"""Fixtures and helpers for grader tests."""

from __future__ import annotations

from uuid import UUID

import pytest

from props.core.db.examples import Example
from props.core.db.models import AgentRun, AgentRunStatus, ReportedIssue
from props.core.db.session import get_session
from props.core.models.examples import WholeSnapshotExample
from props.testing.fixtures import make_critic_run, make_grader_run


def make_test_critic_run(example: Example, num_issues: int = 1) -> UUID:
    """Create a test critic run with specified number of input issues.

    Args:
        example: Example object (snapshot + scope)
        num_issues: Number of input issues to create (default: 1)

    Returns:
        critic_run_id (UUID)
    """

    with get_session() as session:
        # Merge example into this session if it's detached from another session
        example = session.merge(example)

        # Create critic run
        critic_run = make_critic_run(example=example, status=AgentRunStatus.COMPLETED)
        session.add(critic_run)
        session.flush()

        # Populate normalized reported_issues table directly
        for i in range(1, num_issues + 1):
            issue_id = f"input-{i:03d}"
            reported_issue = ReportedIssue(
                agent_run_id=critic_run.agent_run_id, issue_id=issue_id, rationale=f"Test input issue {i}"
            )
            session.add(reported_issue)

        session.commit()

        # Explicitly type the return value to help mypy
        critic_run_id: UUID = critic_run.agent_run_id
        return critic_run_id


def make_test_grader_run(critic_run_id: UUID, status: AgentRunStatus = AgentRunStatus.COMPLETED) -> UUID:
    """Create a test grader run.

    Args:
        critic_run_id: Critic run ID
        status: Run status (default: COMPLETED)

    Returns:
        grader_run_id (UUID)
    """
    with get_session() as session:
        # Fetch the critic_run to pass to factory
        critic_run = session.query(AgentRun).filter_by(agent_run_id=critic_run_id).one()

        # Use centralized factory
        grader_run = make_grader_run(critic_run=critic_run, status=status)
        session.add(grader_run)
        session.commit()
        return grader_run.agent_run_id


# =============================================================================
# Shared test fixtures (used by multiple test files)
# =============================================================================


@pytest.fixture
def test_grader_critic_run(test_db, test_snapshot):
    """Create test critic run with 3 input issues.

    Returns:
        critic_run_id (UUID)
    """
    # Get example from git fixtures
    with get_session() as session:
        example = Example.from_spec(session, WholeSnapshotExample(snapshot_slug=test_snapshot))
    return make_test_critic_run(example, num_issues=3)


@pytest.fixture
def test_grader_run(test_db, test_snapshot, test_grader_critic_run):
    """Create test grader run in IN_PROGRESS status.

    Returns:
        grader_run_id (UUID)
    """
    return make_test_grader_run(test_grader_critic_run, status=AgentRunStatus.IN_PROGRESS)
