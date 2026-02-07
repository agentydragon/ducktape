"""Integration tests for LLM proxy budget enforcement.

Tests that _check_budget correctly allows requests under budget and rejects
when budget is exceeded. Uses real Postgres with llm_request_costs view.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException

from props.backend.routes.llm import _check_budget, _extract_token_usage
from props.core.agent_types import CriticTypeConfig
from props.core.models.examples import WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, LLMRequest
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions

pytestmark = [pytest.mark.integration]

# Real model from model_metadata.yaml (synced by synced_db fixture).
# gpt-5.1: $1.25/M input, $0.125/M cached, $10/M output
TEST_MODEL = "gpt-5.1"

TRAIN_EXAMPLE = WholeSnapshotExample(snapshot_slug="test-fixtures/train1")


def test_budget_allows_under_budget(synced_db: Database) -> None:
    """Request under budget should pass without raising."""
    run_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.add(
            AgentRun(
                agent_run_id=run_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=TEST_MODEL,
                type_config=CriticTypeConfig(example=TRAIN_EXAMPLE),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=10.0,
            )
        )
        session.commit()

    # No requests logged yet — budget should be fine
    with synced_db.session() as session:
        _check_budget(session, run_id, budget_usd=10.0)


def test_budget_rejects_over_budget(synced_db: Database) -> None:
    """Request over budget should raise HTTPException(429)."""
    run_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.add(
            AgentRun(
                agent_run_id=run_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=TEST_MODEL,
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
                model=TEST_MODEL,
                request_body={"model": TEST_MODEL},
                response_body={},
                input_tokens=1_000_000,
                cached_input_tokens=0,
                output_tokens=0,
                latency_ms=100,
            )
        )
        session.commit()

    with synced_db.session() as session:
        with pytest.raises(HTTPException) as exc_info:
            _check_budget(session, run_id, budget_usd=0.01)
        assert exc_info.value.status_code == 429
        assert "Budget exceeded" in exc_info.value.detail


def test_budget_includes_child_run_costs(synced_db: Database) -> None:
    """Budget check should include costs from child (descendant) runs."""
    parent_id = uuid4()
    child_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)

        session.add(
            AgentRun(
                agent_run_id=parent_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=TEST_MODEL,
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
                model=TEST_MODEL,
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
                model=TEST_MODEL,
                request_body={"model": TEST_MODEL},
                response_body={},
                input_tokens=500_000,
                cached_input_tokens=0,
                output_tokens=0,
                latency_ms=100,
            )
        )
        session.commit()

    # Parent budget check should see child's costs
    with synced_db.session() as session:
        with pytest.raises(HTTPException) as exc_info:
            _check_budget(session, parent_id, budget_usd=0.10)
        assert exc_info.value.status_code == 429


def test_extract_token_usage_from_responses_api() -> None:
    """Token extraction from OpenAI Responses API format."""
    response = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 20},
        }
    }
    input_tokens, cached, output_tokens = _extract_token_usage(response)
    assert input_tokens == 100
    assert cached == 20
    assert output_tokens == 50


def test_extract_token_usage_missing_usage() -> None:
    """Missing usage field returns None for all token counts."""
    assert _extract_token_usage({"id": "resp_123"}) == (None, None, None)
    assert _extract_token_usage(None) == (None, None, None)


if __name__ == "__main__":
    pytest_bazel.main()
