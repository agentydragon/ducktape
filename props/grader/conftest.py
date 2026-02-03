"""Fixtures and helpers for grader tests."""

from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from props.core.models.examples import ExampleSpec, WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, ReportedIssue
from props.testing.fixtures.runs import make_fake_critic_run, make_fake_grader_run


@pytest.fixture
def session(synced_db: Database) -> Generator[Session]:
    """Alias for synced_test_session - provides a database session over synced test DB."""
    with synced_db.session() as sess:
        yield sess


def make_test_critic_run(db: Database, example: ExampleSpec, num_issues: int = 1) -> UUID:
    """Create a test critic run with specified number of input issues.

    Returns:
        critic_run_id (UUID)
    """
    with db.session() as session:
        critic_run = make_fake_critic_run(session=session, example=example, status=AgentRunStatus.COMPLETED)
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


def make_test_grader_run(db: Database, critic_run_id: UUID, status: AgentRunStatus = AgentRunStatus.COMPLETED) -> UUID:
    """Create a test grader run.

    Args:
        db: Database instance
        critic_run_id: Critic run ID
        status: Run status (default: COMPLETED)

    Returns:
        grader_run_id (UUID)
    """
    with db.session() as session:
        # Fetch the critic_run to get its snapshot_slug
        critic_run = session.query(AgentRun).filter_by(agent_run_id=critic_run_id).one()
        snapshot_slug = critic_run.critic_config().example.snapshot_slug

        grader_run = make_fake_grader_run(session=session, snapshot_slug=snapshot_slug, status=status)
        session.add(grader_run)
        session.commit()
        return grader_run.agent_run_id


# =============================================================================
# Shared test fixtures (used by multiple test files)
# =============================================================================


@pytest.fixture
def test_grader_critic_run(synced_db: Database, test_snapshot):
    """Create test critic run with 3 input issues.

    Returns:
        critic_run_id (UUID)
    """
    return make_test_critic_run(synced_db, WholeSnapshotExample(snapshot_slug=test_snapshot), num_issues=3)


@pytest.fixture
def test_grader_run(synced_db: Database, test_snapshot, test_grader_critic_run):
    """Create test grader run in IN_PROGRESS status.

    Returns:
        grader_run_id (UUID)
    """
    return make_test_grader_run(synced_db, test_grader_critic_run, status=AgentRunStatus.IN_PROGRESS)
