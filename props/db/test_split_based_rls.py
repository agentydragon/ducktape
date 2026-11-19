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

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from uuid import UUID

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
    ModelMetadata,
    OccurrenceRangeORM,
    Snapshot,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.orchestration.agent_credentials import AgentCredentials
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.credentials import make_agent_credentials
from props.testing.fixtures.runs import FAKE_CRITIC_DEV_OPTIMIZE_DIGEST, FAKE_GRADER_DIGEST, make_fake_critic_run

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")


@pytest_asyncio.fixture
async def critic_dev_optimize_creds(synced_db: Database) -> AsyncGenerator[AgentCredentials]:
    """Create critic-dev agent credentials with a real Postgres role."""
    type_config = CriticDevOptimizeTypeConfig(
        target_metric=TargetMetric.TARGETED, optimizer_model="test-optimizer-model", critic_model="test-critic-model"
    )
    yield await make_agent_credentials(synced_db, type_config, FAKE_CRITIC_DEV_OPTIMIZE_DIGEST)


@contextmanager
def _role_session(synced_db: Database, creds: AgentCredentials) -> Iterator[Session]:
    """Database session connected as the agent's own Postgres role, with its RLS policies active."""
    engine = create_engine(synced_db.config.with_user(creds.username, creds.password).url)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def critic_dev_optimize_session(
    critic_dev_optimize_creds: AgentCredentials, synced_db: Database
) -> AsyncGenerator[Session]:
    """Database session as the critic-dev role."""
    with _role_session(synced_db, critic_dev_optimize_creds) as session:
        yield session


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


async def test_critic_dev_optimize_can_read_model_metadata(synced_db: Database, critic_dev_optimize_session: Session):
    """Agent roles can read model metadata needed to pick the LLM API shape."""
    metadata = critic_dev_optimize_session.get(ModelMetadata, DEFAULT_TEST_MODEL)

    assert metadata is not None
    assert metadata.model_id == DEFAULT_TEST_MODEL


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
    """Critic-dev optimize users see exactly the TRAIN split false positives the admin sees (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures. A policy that hid every row
    would leave the valid1 negative tests passing vacuously, so this is their positive anchor.
    """
    with synced_db.session() as session:
        expected = {fp.fp_id for fp in session.query(FalsePositive).filter_by(snapshot_slug="test-fixtures/train1")}
    assert expected, "train1 specimen must have FPs (check specimen sync)"

    visible = {
        fp.fp_id
        for fp in critic_dev_optimize_session.query(FalsePositive).filter_by(snapshot_slug="test-fixtures/train1")
    }
    assert visible == expected, "critic-dev user should see train split false_positives via RLS"


@pytest.fixture
def critic_run_ids_by_split(synced_db: Database) -> dict[str, UUID]:
    """One critic run with one LLM request per split, written as admin. Maps split name to agent_run_id."""
    run_ids: dict[str, UUID] = {}
    with synced_db.session() as session:
        for split, slug in (
            ("train", "test-fixtures/train1"),
            ("valid", "test-fixtures/valid1"),
            ("test", "test-fixtures/test1"),
        ):
            example = session.query(Example).filter_by(snapshot_slug=slug).first()
            assert example, f"{slug} fixture not found"
            run = make_fake_critic_run(session=session, example=example.to_example_spec(), status=AgentRunStatus.EXITED)
            session.add(run)
            session.flush()
            session.add(
                LLMRequest(
                    agent_run_id=run.agent_run_id,
                    model=DEFAULT_TEST_MODEL,
                    request_body={"messages": [{"role": "user", "content": "test"}]},
                )
            )
            run_ids[split] = run.agent_run_id
        session.commit()
    return run_ids


async def test_critic_dev_optimize_sees_only_train_split_runs_and_llm_requests(
    critic_run_ids_by_split: dict[str, UUID], critic_dev_optimize_session: Session
):
    """Critic-dev optimize users see TRAIN critic runs and their LLM requests, not VALID or TEST ones.

    Hiding VALID prevents overfitting: the optimizer gets validation results only as aggregate
    metrics from SECURITY DEFINER functions, never the runs or the LLM calls behind them.

    Rows exist for all three splits (written as admin), so a policy that hides everything fails
    this equality just as one that leaks a split does.
    """
    seeded = list(critic_run_ids_by_split.values())
    visible_runs = {
        run.agent_run_id
        for run in critic_dev_optimize_session.query(AgentRun).filter(AgentRun.agent_run_id.in_(seeded))
    }
    visible_requests = {
        request.agent_run_id
        for request in critic_dev_optimize_session.query(LLMRequest).filter(LLMRequest.agent_run_id.in_(seeded))
    }

    train = critic_run_ids_by_split["train"]
    assert visible_runs == {train}, "critic-dev user should see only the train split critic run via RLS"
    assert visible_requests == {train}, "critic-dev user should see only the train split llm request via RLS"


TRAIN1_SLUG = SnapshotSlug("test-fixtures/train1")
VALID1_SLUG = SnapshotSlug("test-fixtures/valid1")


@pytest_asyncio.fixture
async def grader_train_creds(synced_db: Database) -> AsyncGenerator[AgentCredentials]:
    """Create grader agent credentials scoped to train1 snapshot."""
    type_config = GraderTypeConfig(snapshot_slug=TRAIN1_SLUG)
    yield await make_agent_credentials(synced_db, type_config, FAKE_GRADER_DIGEST)


@pytest_asyncio.fixture
async def grader_train_session(grader_train_creds: AgentCredentials, synced_db: Database) -> AsyncGenerator[Session]:
    """Database session as the grader role scoped to train1."""
    with _role_session(synced_db, grader_train_creds) as session:
        yield session


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
