"""Test split-based RLS policies for optimization agents.

Verifies that prompt optimizer users (agent roles created via
ensure_agent_role) can only access TRAIN split sensitive data
(true_positives, false_positives, agent_runs, llm_requests, etc.), not TEST or VALID.

**Note on snapshots table**: The snapshots table contains only metadata (slug, split,
source info) which is not sensitive. All agents can see all snapshots. Actual data
access control is enforced on examples, true_positives, false_positives, agent_runs,
and llm_requests tables.

This is distinct from run-based isolation (see clustering/test_rls_isolation.py),
which isolates concurrent runs within the same split.

These tests use per-test isolated databases and require:
- postgres container running (managed by devenv)
- Database environment variables set (PG* vars for admin access)

Each test gets its own database (created and destroyed by test_db fixture).
For RLS testing, tests use:
- admin_user (via get_session()) to write test data
- prompt_optimizer temporary user to verify split-based RLS policies

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

from props.core.agent_types import PromptOptimizerTypeConfig
from props.critic_dev.shared import TargetMetric
from props.db.agent_definition_ids import PROMPT_OPTIMIZER_IMAGE_REF
from props.db.config import DatabaseConfig
from props.db.examples import Example
from props.db.models import AgentRun, AgentRunStatus, FalsePositive, LLMRequest, Snapshot, TruePositive
from props.db.session import get_session
from props.orchestration.agent_credentials import AgentCredentials, ensure_agent_role
from props.testing.fixtures.runs import make_critic_run

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest_asyncio.fixture
async def prompt_optimizer_creds(synced_test_db: DatabaseConfig) -> AsyncGenerator[AgentCredentials]:
    """Create prompt optimizer agent credentials.

    Creates an AgentRun record with agent_type='prompt_optimizer' so that
    current_agent_type() returns the correct value for RLS policy evaluation.

    Returns:
        credentials for use in RLS tests
    """
    run_id = uuid4()

    # Create AgentRun record with prompt_optimizer type_config (as admin)
    # This must happen BEFORE the agent role is created, so RLS policies can
    # identify this user as a prompt_optimizer via current_agent_type()
    with get_session() as session:
        type_config = PromptOptimizerTypeConfig(
            target_metric=TargetMetric.TARGETED,
            optimizer_model="test-optimizer-model",
            critic_model="test-critic-model",
            grader_model="test-grader-model",
            budget_limit=100.0,
        )
        agent_run = AgentRun(
            agent_run_id=run_id,
            image_digest=PROMPT_OPTIMIZER_IMAGE_REF,
            model="test-model",
            status=AgentRunStatus.COMPLETED,
            type_config=type_config.model_dump(),
        )
        session.add(agent_run)
        session.commit()

    yield await ensure_agent_role(synced_test_db.admin, run_id)


@pytest_asyncio.fixture
async def prompt_optimizer_session(
    prompt_optimizer_creds: AgentCredentials, synced_test_db: DatabaseConfig
) -> AsyncGenerator[Session]:
    """Create database session as prompt optimizer temp user.

    Yields session with RLS policies active for prompt optimizer role.
    """
    user_config = synced_test_db.admin.with_user(prompt_optimizer_creds.username, prompt_optimizer_creds.password)
    engine = create_engine(user_config.url())

    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


async def test_prompt_optimizer_can_see_all_snapshots_metadata(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users CAN see all snapshots including TEST split.

    The snapshots table contains only metadata (slug, split, source info) which
    is not sensitive. All agents can see all snapshots. Actual data access
    control is enforced on sensitive tables (true_positives, agent_runs, etc.).

    Uses test-fixtures/test1 (TEST split) from git fixtures.
    """
    # Can see TEST split snapshots (metadata only, not sensitive)
    test_snapshots = prompt_optimizer_session.query(Snapshot).filter(Snapshot.slug == "test-fixtures/test1").all()
    assert len(test_snapshots) == 1, "prompt optimizer user CAN see all snapshots metadata"
    assert test_snapshots[0].split == "test"

    # Can also see TRAIN and VALID snapshots
    all_snapshots = prompt_optimizer_session.query(Snapshot).all()
    splits = {s.split for s in all_snapshots}
    assert "train" in splits
    assert "valid" in splits
    assert "test" in splits


async def test_prompt_optimizer_can_see_train_split_snapshots(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users can see TRAIN split snapshots (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.

    Setup (as admin_user):
    - Git fixture already has test-trivial snapshot

    Verify (as prompt optimizer temp user):
    - Can query snapshots for train split
    """
    train_snapshots = prompt_optimizer_session.query(Snapshot).filter(Snapshot.slug == "test-fixtures/train1").all()

    assert len(train_snapshots) == 1, "prompt optimizer user should see train split snapshots via RLS"
    assert train_snapshots[0].split == "train"


async def test_prompt_optimizer_cannot_see_valid_split_true_positives(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users CANNOT see valid split true positives (RLS policy blocks).

    Uses test-fixtures/valid1 (VALID split) from git fixtures with synced TPs.

    Setup (as admin_user):
    - Git fixture already has test-validation snapshot with TPs

    Verify (as prompt optimizer temp user):
    - CANNOT query true positives for valid specimens (returns 0 rows)
    """
    # Should NOT see true positives for valid specimen
    valid_tps = (
        prompt_optimizer_session.query(TruePositive).filter(TruePositive.snapshot_slug == "test-fixtures/valid1").all()
    )
    assert len(valid_tps) == 0, "prompt optimizer user should NOT see valid split true_positives via RLS"


async def test_prompt_optimizer_can_see_train_split_false_positives(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users can see TRAIN split false positives (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.
    Note: test-trivial may not have FPs, but the test verifies RLS allows the query.

    Setup (as admin_user):
    - Git fixture already has test-trivial snapshot

    Verify (as prompt optimizer temp user):
    - Can query false positives for train specimens (query succeeds, no RLS block)
    """
    # Query should succeed (no RLS block), but may return empty if no FPs defined
    _ = (
        prompt_optimizer_session.query(FalsePositive)
        .filter(FalsePositive.snapshot_slug == "test-fixtures/train1")
        .all()
    )
    # Just verify query succeeded (no exception from RLS block)
    # Not asserting specific count since test-trivial may not have FPs


async def test_prompt_optimizer_cannot_see_test_split_critic_runs(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users cannot see TEST split critic runs (RLS policy blocks).

    Uses test-fixtures/test1 (TEST split) from git fixtures.

    Setup (as admin_user):
    - Query existing test-split-test snapshot and example
    - Create critic run for test snapshot

    Verify (as prompt optimizer temp user):
    - Cannot query critic_runs for test split specimens
    """
    # Setup: Use admin_user to write test data
    with get_session() as session:
        # Query git fixture example (TEST split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/test1").first()
        assert example, "test-split-test fixture not found"

        # Create a critic run for the test specimen using fixture factory
        test_run = make_critic_run(example=example, status=AgentRunStatus.COMPLETED)
        session.add(test_run)
        session.commit()

    # Verify: Connect as prompt optimizer temp user and verify RLS blocks test split
    test_runs = (
        prompt_optimizer_session.query(AgentRun)
        .filter(AgentRun.type_config["snapshot_slug"].astext == "test-fixtures/test1")
        .all()
    )

    assert len(test_runs) == 0, "prompt optimizer user should not see test split critic_runs via RLS"


async def test_prompt_optimizer_can_see_train_split_critic_runs(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users can see TRAIN split critic runs (RLS policy allows).

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.

    Setup (as admin_user):
    - Query existing test-trivial snapshot and example
    - Create critic run for train snapshot

    Verify (as prompt optimizer temp user):
    - Can query critic_runs for train split specimens
    """
    # Setup: Use admin_user to write test data
    train_agent_run_id = uuid4()

    with get_session() as session:
        # Query git fixture example (TRAIN split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/train1").first()
        assert example, "test-trivial fixture not found"

        # Create a critic run for the train specimen using fixture factory
        train_run = make_critic_run(example=example, agent_run_id=train_agent_run_id, status=AgentRunStatus.COMPLETED)
        session.add(train_run)
        session.commit()

    # Verify: Connect as prompt optimizer temp user and verify can see train split
    train_runs = prompt_optimizer_session.query(AgentRun).filter(AgentRun.agent_run_id == train_agent_run_id).all()

    assert len(train_runs) == 1, "prompt optimizer user should see train split critic_runs via RLS"
    assert train_runs[0].critic_config().example.snapshot_slug == "test-fixtures/train1"


async def test_prompt_optimizer_cannot_see_valid_split_critic_runs(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users CANNOT see VALID split critic runs (RLS policy blocks).

    This prevents overfitting - the optimizer cannot inspect validation run details,
    only aggregate metrics via SECURITY DEFINER functions.

    Uses test-fixtures/valid1 (VALID split) from git fixtures.
    """
    valid_agent_run_id = uuid4()

    # Setup: Use admin_user to write test data
    with get_session() as session:
        # Query git fixture example (VALID split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/valid1").first()
        assert example, "test-validation fixture not found"

        # Create a critic run for the valid specimen
        valid_run = make_critic_run(example=example, agent_run_id=valid_agent_run_id, status=AgentRunStatus.COMPLETED)
        session.add(valid_run)
        session.commit()

    # Verify: Connect as prompt optimizer temp user and verify RLS blocks valid split
    valid_runs = (
        prompt_optimizer_session.query(AgentRun)
        .filter(AgentRun.type_config["snapshot_slug"].astext == "test-fixtures/valid1")
        .all()
    )

    assert len(valid_runs) == 0, "prompt optimizer user should NOT see valid split critic_runs via RLS"


async def test_prompt_optimizer_cannot_see_valid_split_llm_requests(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users CANNOT see VALID split LLM requests (RLS policy blocks).

    This prevents learning from validation failures - the optimizer cannot inspect
    what LLM calls the critic made during validation runs.

    Uses test-fixtures/valid1 (VALID split) from git fixtures.
    """
    valid_agent_run_id = uuid4()

    # Setup: Use admin_user to write test data
    with get_session() as session:
        # Query git fixture example (VALID split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/valid1").first()
        assert example, "test-validation fixture not found"

        # Create a critic run for the valid specimen
        valid_run = make_critic_run(example=example, agent_run_id=valid_agent_run_id, status=AgentRunStatus.COMPLETED)
        session.add(valid_run)
        session.flush()

        # Add an LLM request for this run
        llm_request = LLMRequest(
            agent_run_id=valid_agent_run_id,
            model="gpt-4o",
            request_body={"messages": [{"role": "user", "content": "test"}]},
        )
        session.add(llm_request)
        session.commit()

    # Verify: Connect as prompt optimizer temp user and verify RLS blocks requests
    valid_requests = (
        prompt_optimizer_session.query(LLMRequest).filter(LLMRequest.agent_run_id == valid_agent_run_id).all()
    )

    assert len(valid_requests) == 0, "prompt optimizer user should NOT see valid split llm_requests via RLS"


async def test_prompt_optimizer_can_see_train_split_llm_requests(
    synced_test_db: DatabaseConfig, prompt_optimizer_session: Session
):
    """Prompt optimizer users CAN see TRAIN split LLM requests (RLS policy allows).

    The optimizer can inspect training run details to understand failures and improve prompts.

    Uses test-fixtures/train1 (TRAIN split) from git fixtures.
    """
    train_agent_run_id = uuid4()

    # Setup: Use admin_user to write test data
    with get_session() as session:
        # Query git fixture example (TRAIN split)
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/train1").first()
        assert example, "test-trivial fixture not found"

        # Create a critic run for the train specimen
        train_run = make_critic_run(example=example, agent_run_id=train_agent_run_id, status=AgentRunStatus.COMPLETED)
        session.add(train_run)
        session.flush()

        # Add an LLM request for this run
        llm_request = LLMRequest(
            agent_run_id=train_agent_run_id,
            model="gpt-4o",
            request_body={"messages": [{"role": "user", "content": "test"}]},
        )
        session.add(llm_request)
        session.commit()

    # Verify: Connect as prompt optimizer temp user and verify can see train split requests
    train_requests = (
        prompt_optimizer_session.query(LLMRequest).filter(LLMRequest.agent_run_id == train_agent_run_id).all()
    )

    assert len(train_requests) == 1, "prompt optimizer user should see train split llm_requests via RLS"
    assert train_requests[0].model == "gpt-4o"


if __name__ == "__main__":
    pytest_bazel.main()
