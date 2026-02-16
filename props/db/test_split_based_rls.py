"""Test split-based RLS policies for agent roles.

Verifies that agent roles (critic-dev, grader) created via ensure_agent_role
respect RLS policies:
- Critic-dev: can only access TRAIN split sensitive data, not TEST or VALID.
- Grader: can access ground truth tables (occurrence_ranges, true_positives, etc.)
  for its snapshot.

**Note on snapshots table**: The snapshots table contains only metadata (slug, split,
source info) which is not sensitive. All agents can see all snapshots. Actual data
access control is enforced on examples, true_positives, false_positives, agent_runs,
and llm_requests tables.

This is distinct from run-based isolation (see clustering/test_rls_isolation.py),
which isolates concurrent runs within the same split.

These tests use per-test isolated databases and require:
- postgres container running (managed by devenv)

Each test gets its own database (created and destroyed by db fixture).
For RLS testing, tests use:
- admin_user (via db.session()) to write test data
- critic_dev_optimize / grader temporary users to verify split-based RLS policies

Note: These tests share a module-scoped fixture and work correctly with pytest-xdist
because the project uses --dist=loadscope by default, which ensures all tests in
this module run in the same worker process.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
import pytest_bazel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from props.core.agent_types import CriticDevOptimizeTypeConfig, GraderTypeConfig, TargetMetric
from props.core.ids import SnapshotSlug
from props.db.database import Database
from props.db.examples import Example
from props.db.models import (
    AgentRun,
    AgentRunStatus,
    FalsePositive,
    LLMRequest,
    OccurrenceRangeORM,
    Snapshot,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.orchestration.agent_credentials import AgentCredentials
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.credentials import make_agent_credentials
from props.testing.fixtures.runs import FAKE_CRITIC_DEV_OPTIMIZE_DIGEST, FAKE_GRADER_DIGEST, make_fake_critic_run

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def critic_dev_optimize_creds(synced_db: Database) -> AsyncGenerator[AgentCredentials]:
    """Create critic-dev agent credentials with a real Postgres role."""
    type_config = CriticDevOptimizeTypeConfig(
        target_metric=TargetMetric.TARGETED, optimizer_model="test-optimizer-model", critic_model="test-critic-model"
    )
    yield await make_agent_credentials(synced_db, type_config, FAKE_CRITIC_DEV_OPTIMIZE_DIGEST)


@pytest_asyncio.fixture
async def critic_dev_optimize_session(
    critic_dev_optimize_creds: AgentCredentials, synced_db: Database
) -> AsyncGenerator[Session]:
    """Create database session as critic-dev temp user.

    Yields session with RLS policies active for critic-dev role.
    """
    user_config = synced_db.config.with_user(critic_dev_optimize_creds.username, critic_dev_optimize_creds.password)
    engine = create_engine(user_config.url)

    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


async def test_critic_dev_optimize_can_see_all_snapshots_metadata(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users CAN see all snapshots including TEST split.

    The snapshots table contains only metadata (slug, split, source info) which
    is not sensitive. All agents can see all snapshots. Actual data access
    control is enforced on sensitive tables (true_positives, agent_runs, etc.).

    Uses test-fixtures/test1 (TEST split) from git fixtures.
    """
    # Can see TEST split snapshots (metadata only, not sensitive)
    test_snapshots = critic_dev_optimize_session.query(Snapshot).filter(Snapshot.slug == "test-fixtures/test1").all()
    assert len(test_snapshots) == 1, "critic-dev user CAN see all snapshots metadata"
    assert test_snapshots[0].split == "test"

    # Can also see TRAIN and VALID snapshots
    all_snapshots = critic_dev_optimize_session.query(Snapshot).all()
    splits = {s.split for s in all_snapshots}
    assert "train" in splits
    assert "valid" in splits
    assert "test" in splits


async def test_critic_dev_optimize_can_see_train_split_snapshots(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users can see TRAIN split snapshots (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.

    Setup (as admin_user):
    - Git fixture already has test-trivial snapshot

    Verify (as critic-dev temp user):
    - Can query snapshots for train split
    """
    train_snapshots = critic_dev_optimize_session.query(Snapshot).filter(Snapshot.slug == "test-fixtures/train1").all()

    assert len(train_snapshots) == 1, "critic-dev user should see train split snapshots via RLS"
    assert train_snapshots[0].split == "train"


async def test_critic_dev_optimize_cannot_see_valid_split_true_positives(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users CANNOT see valid split true positives (RLS policy blocks).

    Uses test-fixtures/valid1 (VALID split) from git fixtures with synced TPs.

    Setup (as admin_user):
    - Git fixture already has test-validation snapshot with TPs

    Verify (as critic-dev temp user):
    - CANNOT query true positives for valid specimens (returns 0 rows)
    """
    # Should NOT see true positives for valid specimen
    valid_tps = (
        critic_dev_optimize_session.query(TruePositive)
        .filter(TruePositive.snapshot_slug == "test-fixtures/valid1")
        .all()
    )
    assert len(valid_tps) == 0, "critic-dev user should NOT see valid split true_positives via RLS"


async def test_critic_dev_optimize_can_see_train_split_false_positives(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users can see TRAIN split false positives (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.
    Note: test-trivial may not have FPs, but the test verifies RLS allows the query.

    Setup (as admin_user):
    - Git fixture already has test-trivial snapshot

    Verify (as critic-dev temp user):
    - Can query false positives for train specimens (query succeeds, no RLS block)
    """
    # Query should succeed (no RLS block), but may return empty if no FPs defined
    _ = (
        critic_dev_optimize_session.query(FalsePositive)
        .filter(FalsePositive.snapshot_slug == "test-fixtures/train1")
        .all()
    )
    # Just verify query succeeded (no exception from RLS block)
    # Not asserting specific count since test-trivial may not have FPs


async def test_critic_dev_optimize_cannot_see_test_split_critic_runs(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users cannot see TEST split critic runs (RLS policy blocks).

    Uses test-fixtures/test1 (TEST split) from git fixtures.

    Setup (as admin_user):
    - Query existing test-split-test snapshot and example
    - Create critic run for test snapshot

    Verify (as critic-dev temp user):
    - Cannot query critic_runs for test split specimens
    """
    # Setup: Use admin_user to write test data
    with synced_db.session() as session:
        # Query git fixture example (TEST split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/test1").first()
        assert example, "test-split-test fixture not found"

        # Create a critic run for the test specimen using fixture factory
        test_run = make_fake_critic_run(
            session=session, example=example.to_example_spec(), status=AgentRunStatus.EXITED
        )
        session.add(test_run)
        session.commit()

    # Verify: Connect as critic-dev temp user and verify RLS blocks test split
    test_runs = (
        critic_dev_optimize_session.query(AgentRun)
        .filter(AgentRun.type_config["snapshot_slug"].astext == "test-fixtures/test1")
        .all()
    )

    assert len(test_runs) == 0, "critic-dev user should not see test split critic_runs via RLS"


async def test_critic_dev_optimize_can_see_train_split_critic_runs(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users can see TRAIN split critic runs (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.

    Setup (as admin_user):
    - Query existing test-trivial snapshot and example
    - Create critic run for train snapshot

    Verify (as critic-dev temp user):
    - Can query critic_runs for train split specimens
    """
    # Setup: Use admin_user to write test data
    train_agent_run_id = uuid4()

    with synced_db.session() as session:
        # Query git fixture example (TRAIN split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/train1").first()
        assert example, "test-trivial fixture not found"

        # Create a critic run for the train specimen using fixture factory
        train_run = make_fake_critic_run(
            session=session,
            example=example.to_example_spec(),
            agent_run_id=train_agent_run_id,
            status=AgentRunStatus.EXITED,
        )
        session.add(train_run)
        session.commit()

    # Verify: Connect as critic-dev temp user and verify can see train split
    train_runs = critic_dev_optimize_session.query(AgentRun).filter(AgentRun.agent_run_id == train_agent_run_id).all()

    assert len(train_runs) == 1, "critic-dev user should see train split critic_runs via RLS"
    assert train_runs[0].critic_config().example.snapshot_slug == "test-fixtures/train1"


async def test_critic_dev_optimize_cannot_see_valid_split_critic_runs(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users CANNOT see VALID split critic runs (RLS policy blocks).

    This prevents overfitting - the optimizer cannot inspect validation run details,
    only aggregate metrics via SECURITY DEFINER functions.

    Uses test-fixtures/valid1 (VALID split) from git fixtures.
    """
    valid_agent_run_id = uuid4()

    # Setup: Use admin_user to write test data
    with synced_db.session() as session:
        # Query git fixture example (VALID split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/valid1").first()
        assert example, "test-validation fixture not found"

        # Create a critic run for the valid specimen
        valid_run = make_fake_critic_run(
            session=session,
            example=example.to_example_spec(),
            agent_run_id=valid_agent_run_id,
            status=AgentRunStatus.EXITED,
        )
        session.add(valid_run)
        session.commit()

    # Verify: Connect as critic-dev temp user and verify RLS blocks valid split
    valid_runs = (
        critic_dev_optimize_session.query(AgentRun)
        .filter(AgentRun.type_config["snapshot_slug"].astext == "test-fixtures/valid1")
        .all()
    )

    assert len(valid_runs) == 0, "critic-dev user should NOT see valid split critic_runs via RLS"


async def test_critic_dev_optimize_cannot_see_valid_split_llm_requests(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users CANNOT see VALID split LLM requests (RLS policy blocks).

    This prevents learning from validation failures - the optimizer cannot inspect
    what LLM calls the critic made during validation runs.

    Uses test-fixtures/valid1 (VALID split) from git fixtures.
    """
    valid_agent_run_id = uuid4()

    # Setup: Use admin_user to write test data
    with synced_db.session() as session:
        # Query git fixture example (VALID split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/valid1").first()
        assert example, "test-validation fixture not found"

        # Create a critic run for the valid specimen
        valid_run = make_fake_critic_run(
            session=session,
            example=example.to_example_spec(),
            agent_run_id=valid_agent_run_id,
            status=AgentRunStatus.EXITED,
        )
        session.add(valid_run)
        session.flush()

        # Add an LLM request for this run
        llm_request = LLMRequest(
            agent_run_id=valid_agent_run_id,
            model=DEFAULT_TEST_MODEL,
            request_body={"messages": [{"role": "user", "content": "test"}]},
        )
        session.add(llm_request)
        session.commit()

    # Verify: Connect as critic-dev temp user and verify RLS blocks requests
    valid_requests = (
        critic_dev_optimize_session.query(LLMRequest).filter(LLMRequest.agent_run_id == valid_agent_run_id).all()
    )

    assert len(valid_requests) == 0, "critic-dev user should NOT see valid split llm_requests via RLS"


async def test_critic_dev_optimize_can_see_train_split_llm_requests(
    synced_db: Database, critic_dev_optimize_session: Session
):
    """Critic-dev optimize users CAN see TRAIN split LLM requests (RLS policy allows).

    The optimizer can inspect training run details to understand failures and improve prompts.

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.
    """
    train_agent_run_id = uuid4()

    # Setup: Use admin_user to write test data
    with synced_db.session() as session:
        # Query git fixture example (TRAIN split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/train1").first()
        assert example, "test-trivial fixture not found"

        # Create a critic run for the train specimen
        train_run = make_fake_critic_run(
            session=session,
            example=example.to_example_spec(),
            agent_run_id=train_agent_run_id,
            status=AgentRunStatus.EXITED,
        )
        session.add(train_run)
        session.flush()

        # Add an LLM request for this run
        llm_request = LLMRequest(
            agent_run_id=train_agent_run_id,
            model=DEFAULT_TEST_MODEL,
            request_body={"messages": [{"role": "user", "content": "test"}]},
        )
        session.add(llm_request)
        session.commit()

    # Verify: Connect as critic-dev temp user and verify can see train split requests
    train_requests = (
        critic_dev_optimize_session.query(LLMRequest).filter(LLMRequest.agent_run_id == train_agent_run_id).all()
    )

    assert len(train_requests) == 1, "critic-dev user should see train split llm_requests via RLS"
    assert train_requests[0].model == DEFAULT_TEST_MODEL


# =============================================================================
# Grader RLS tests — ground truth table access
# =============================================================================

TRAIN1_SLUG = SnapshotSlug("test-fixtures/train1")
VALID1_SLUG = SnapshotSlug("test-fixtures/valid1")


@pytest_asyncio.fixture
async def grader_train_creds(synced_db: Database) -> AsyncGenerator[AgentCredentials]:
    """Create grader agent credentials scoped to train1 snapshot."""
    type_config = GraderTypeConfig(snapshot_slug=TRAIN1_SLUG)
    yield await make_agent_credentials(synced_db, type_config, FAKE_GRADER_DIGEST)


@pytest_asyncio.fixture
async def grader_train_session(grader_train_creds: AgentCredentials, synced_db: Database) -> AsyncGenerator[Session]:
    """Database session as grader role scoped to train1."""
    user_config = synced_db.config.with_user(grader_train_creds.username, grader_train_creds.password)
    engine = create_engine(user_config.url)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


async def test_grader_can_read_occurrence_ranges_with_content(synced_db: Database, grader_train_session: Session):
    """Grader agent can SELECT from occurrence_ranges and get actual GT content.

    Regression test for a bug where occurrence_ranges was missing from GRANT,
    RLS enable, and RLS policy lists — causing grader show_tp/show_fp tools to
    return 'permission denied' instead of actual line range data.

    Verifies that file_path, start_line, end_line are returned (not just empty
    results or permission errors).
    """
    rows = grader_train_session.query(OccurrenceRangeORM).filter(OccurrenceRangeORM.snapshot_slug == TRAIN1_SLUG).all()

    # train1 has 7 occurrence_ranges rows (5 TPs with varying file counts)
    assert len(rows) >= 1, "Grader must see occurrence_ranges rows for its snapshot"

    # Verify actual content is returned, not empty/null placeholders
    for row in rows:
        assert row.file_path is not None, "file_path must not be None"
        assert str(row.file_path) != "", "file_path must not be empty"
        assert row.start_line is not None, "start_line must not be None"
        assert row.end_line is not None, "end_line must not be None"
        assert row.start_line > 0, "start_line must be positive"
        assert row.end_line >= row.start_line, "end_line must be >= start_line"
        assert row.occurrence_id is not None, "occurrence_id must not be None"
        # Exactly one of tp_id/fp_id must be set (exclusive arc)
        assert (row.tp_id is None) != (row.fp_id is None), "Exclusive arc: exactly one of tp_id/fp_id must be set"


async def test_grader_can_read_tp_occurrences(synced_db: Database, grader_train_session: Session):
    """Grader agent can SELECT from true_positive_occurrences for its snapshot."""
    rows = (
        grader_train_session.query(TruePositiveOccurrenceORM)
        .filter(TruePositiveOccurrenceORM.snapshot_slug == TRAIN1_SLUG)
        .all()
    )

    assert len(rows) >= 1, "Grader must see TP occurrences for its snapshot"
    for row in rows:
        assert row.tp_id is not None
        assert row.occurrence_id is not None


async def test_grader_cannot_see_other_snapshot_occurrence_ranges(synced_db: Database, grader_train_session: Session):
    """Grader scoped to train1 cannot see occurrence_ranges for valid1."""
    rows = grader_train_session.query(OccurrenceRangeORM).filter(OccurrenceRangeORM.snapshot_slug == VALID1_SLUG).all()

    assert len(rows) == 0, "Grader should NOT see occurrence_ranges for other snapshots"


if __name__ == "__main__":
    pytest_bazel.main()
