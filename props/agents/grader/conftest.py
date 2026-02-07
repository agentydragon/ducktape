"""Fixtures and helpers for grader tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from props.core.models.examples import WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRunStatus
from props.testing.fixtures.runs import make_critic_run_with_issues, make_grader_run_for_critic


@pytest.fixture
def session(synced_db: Database) -> Generator[Session]:
    """Alias for synced_test_session - provides a database session over synced test DB."""
    with synced_db.session() as sess:
        yield sess


# =============================================================================
# Shared test fixtures (used by multiple test files)
# =============================================================================


@pytest.fixture
def test_grader_critic_run(synced_db: Database, test_snapshot):
    """Create test critic run with 3 input issues."""
    return make_critic_run_with_issues(synced_db, WholeSnapshotExample(snapshot_slug=test_snapshot), num_issues=3)


@pytest.fixture
def test_grader_run(synced_db: Database, test_snapshot, test_grader_critic_run):
    """Create test grader run in IN_PROGRESS status."""
    return make_grader_run_for_critic(synced_db, test_grader_critic_run, status=AgentRunStatus.IN_PROGRESS)
