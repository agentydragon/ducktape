"""Integration tests for LLM proxy budget enforcement.

Tests that _check_budget correctly allows requests under budget and rejects
when budget is exceeded. Uses real Postgres with agent_run_budget_status view.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException

from props.core.agent_types import CriticTypeConfig
from props.db.async_database import AsyncDatabase
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, LLMRequest
from props.llm_proxy.routes import _check_budget
from props.testing.constants import BUDGET_TEST_MODEL, TRAIN_EXAMPLE
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_usd", [10.0, 0.0], ids=["positive-budget", "zero-budget"])
async def test_budget_allows_no_spend(synced_db: Database, budget_usd: float) -> None:
    """No spend is allowed with a positive budget and at the zero-budget boundary."""
    run_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.add(
            AgentRun(
                agent_run_id=run_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=BUDGET_TEST_MODEL,
                type_config=CriticTypeConfig(example=TRAIN_EXAMPLE),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=budget_usd,
            )
        )
        session.commit()

    async with AsyncDatabase(synced_db.config) as db, db.session() as session:
        await _check_budget(session, run_id, budget_usd=budget_usd)


@pytest.mark.asyncio
async def test_budget_rejects_over_budget(synced_db: Database) -> None:
    """Request over budget should raise HTTPException(429)."""
    run_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.add(
            AgentRun(
                agent_run_id=run_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=BUDGET_TEST_MODEL,
                type_config=CriticTypeConfig(example=TRAIN_EXAMPLE),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=0.01,
            )
        )
        session.flush()

        # 1M input tokens at $1.25/M = $1.25 (way over $0.01 budget)
        session.add(
            LLMRequest(
                agent_run_id=run_id,
                model=BUDGET_TEST_MODEL,
                request_body={"model": BUDGET_TEST_MODEL},
                response_body={},
                input_tokens=1_000_000,
                cached_input_tokens=0,
                output_tokens=0,
                latency_ms=100,
            )
        )
        session.commit()

    async with AsyncDatabase(synced_db.config) as db:
        async with db.session() as session:
            with pytest.raises(HTTPException) as exc_info:
                await _check_budget(session, run_id, budget_usd=0.01)
        assert exc_info.value.status_code == 429
        assert "Budget exceeded" in exc_info.value.detail


@pytest.mark.asyncio
async def test_budget_includes_child_run_costs(synced_db: Database) -> None:
    """Budget check should include costs from child (descendant) runs."""
    parent_id = uuid4()
    child_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)

        session.add(
            AgentRun(
                agent_run_id=parent_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=BUDGET_TEST_MODEL,
                type_config=CriticTypeConfig(example=TRAIN_EXAMPLE),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=0.10,
            )
        )
        session.flush()

        session.add(
            AgentRun(
                agent_run_id=child_id,
                image_digest=FAKE_CRITIC_DIGEST,
                parent_agent_run_id=parent_id,
                model=BUDGET_TEST_MODEL,
                type_config=CriticTypeConfig(example=TRAIN_EXAMPLE),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=0.05,
            )
        )
        session.flush()

        # 500k input tokens at $1.25/M = $0.625 (over parent's $0.10 budget)
        session.add(
            LLMRequest(
                agent_run_id=child_id,
                model=BUDGET_TEST_MODEL,
                request_body={"model": BUDGET_TEST_MODEL},
                response_body={},
                input_tokens=500_000,
                cached_input_tokens=0,
                output_tokens=0,
                latency_ms=100,
            )
        )
        session.commit()

    # Parent budget check should see child's costs
    async with AsyncDatabase(synced_db.config) as db:
        async with db.session() as session:
            with pytest.raises(HTTPException) as exc_info:
                await _check_budget(session, parent_id, budget_usd=0.10)
        assert exc_info.value.status_code == 429


if __name__ == "__main__":
    pytest_bazel.main()
