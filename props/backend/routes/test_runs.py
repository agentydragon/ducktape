"""Tests for start_critic endpoint and agent_run_budget_status view.

Tests the HTTP error mapping for budget exceeded, image resolution failures,
and the agent_run_budget_status DB view with real Postgres.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from props.backend.auth import AdminIdentity, require_critic_run_access
from props.backend.deps import get_admin_db
from props.backend.routes import runs
from props.core.agent_types import CriticTypeConfig
from props.core.models.examples import WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunBudgetStatus, AgentRunStatus, LLMRequest
from props.orchestration.agent_registry import BudgetExceededError, ImageResolutionError, ResolvedImage
from props.testing.constants import BUDGET_TEST_MODEL
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions

FAKE_RESOLVED = ResolvedImage(digest=FAKE_CRITIC_DIGEST, oci_ref=f"localhost:5000/critic@{FAKE_CRITIC_DIGEST}")

pytestmark = [pytest.mark.integration]

TRAIN_EXAMPLE = WholeSnapshotExample(snapshot_slug="test-fixtures/train1")


@pytest.fixture
def parent_run(synced_db: Database) -> UUID:
    """Create a parent agent run with $2.00 budget, return its ID."""
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
                budget_usd=2.0,
            )
        )
        session.commit()
    return run_id


# --- agent_run_budget_status view tests (real Postgres) ---


def test_budget_view_no_spend(synced_db: Database, parent_run: UUID) -> None:
    """View shows full budget remaining when no LLM requests exist."""
    with synced_db.session() as session:
        status = session.get(AgentRunBudgetStatus, parent_run)
        assert status is not None
        assert status.budget_usd == 2.0
        assert status.own_spent_usd == 0.0
        assert status.tree_spent_usd == 0.0
        assert status.remaining_usd == 2.0


def test_budget_view_with_spend(synced_db: Database, parent_run: UUID) -> None:
    """View subtracts LLM request costs from remaining budget."""
    with synced_db.session() as session:
        # 1M input tokens at $1.25/M = $1.25 spent, leaving $0.75
        session.add(
            LLMRequest(
                agent_run_id=parent_run,
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

    with synced_db.session() as session:
        status = session.get(AgentRunBudgetStatus, parent_run)
        assert status is not None
        assert status.budget_usd == 2.0
        assert status.own_spent_usd == pytest.approx(1.25, abs=0.01)
        assert status.tree_spent_usd == pytest.approx(1.25, abs=0.01)
        assert status.remaining_usd == pytest.approx(0.75, abs=0.01)


def test_budget_view_includes_child_costs(synced_db: Database, parent_run: UUID) -> None:
    """View includes costs from child (descendant) runs in parent's spent."""
    child_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.add(
            AgentRun(
                agent_run_id=child_id,
                image_digest=FAKE_CRITIC_DIGEST,
                parent_agent_run_id=parent_run,
                model=BUDGET_TEST_MODEL,
                type_config=CriticTypeConfig(example=TRAIN_EXAMPLE),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=1.0,
            )
        )
        session.flush()
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

    with synced_db.session() as session:
        status = session.get(AgentRunBudgetStatus, parent_run)
        assert status is not None
        # 500k input tokens at $1.25/M = $0.625
        assert status.own_spent_usd == 0.0  # Parent itself spent nothing
        assert status.tree_spent_usd == pytest.approx(0.625, abs=0.01)
        assert status.remaining_usd == pytest.approx(1.375, abs=0.01)


# --- HTTP endpoint tests (mock registry, real FastAPI) ---


@pytest.fixture
def run_critic_client(synced_db: Database):
    """FastAPI TestClient with mocked registry for testing start_critic endpoint.

    Creates a minimal FastAPI app with just the runs router, overriding
    dependencies to avoid needing Docker/lifespan infrastructure.
    """
    mock_registry = AsyncMock()

    app = FastAPI()
    app.include_router(runs.router, prefix="/api/runs")
    app.dependency_overrides[get_admin_db] = lambda: synced_db
    app.dependency_overrides[require_critic_run_access] = lambda: AdminIdentity(username="admin", password="admin")
    app.state.registry = mock_registry

    client = TestClient(app, raise_server_exceptions=False)
    return client, mock_registry


def test_run_critic_budget_exceeded_returns_422(run_critic_client) -> None:
    """POST /api/runs/critic returns 422 when budget exceeds parent's remaining."""
    client, mock_registry = run_critic_client

    mock_registry.resolve_image.return_value = FAKE_RESOLVED
    mock_registry.start_critic.side_effect = BudgetExceededError(
        "Cannot spawn child with $5.00 budget: parent has $0.75 remaining ($1.25 spent of $2.00)"
    )

    response = client.post(
        "/api/runs/critic",
        json={
            "definition_id": FAKE_CRITIC_DIGEST,
            "example": {"kind": "whole_snapshot", "snapshot_slug": "test-fixtures/train1"},
            "critic_model": BUDGET_TEST_MODEL,
            "timeout_seconds": 60,
            "budget_usd": 5.0,
        },
    )

    assert response.status_code == 422
    assert "Cannot spawn child" in response.json()["detail"]
    assert "$5.00" in response.json()["detail"]
    # Verify resolved image was passed to start_critic
    mock_registry.start_critic.assert_called_once()
    assert mock_registry.start_critic.call_args.kwargs["image"] is FAKE_RESOLVED


def test_run_critic_image_resolution_error_returns_422(run_critic_client) -> None:
    """POST /api/runs/critic returns 422 when image cannot be resolved."""
    client, mock_registry = run_critic_client

    mock_registry.resolve_image.side_effect = ImageResolutionError("Image not found: critic:nonexistent")

    response = client.post(
        "/api/runs/critic",
        json={
            "definition_id": "nonexistent",
            "example": {"kind": "whole_snapshot", "snapshot_slug": "test-fixtures/train1"},
            "critic_model": BUDGET_TEST_MODEL,
            "timeout_seconds": 60,
            "budget_usd": 1.0,
        },
    )

    assert response.status_code == 422
    assert "Image not found" in response.json()["detail"]


def test_run_critic_snapshot_not_found_returns_404(run_critic_client) -> None:
    """POST /api/runs/critic returns 404 when snapshot doesn't exist."""
    client, mock_registry = run_critic_client

    response = client.post(
        "/api/runs/critic",
        json={
            "definition_id": FAKE_CRITIC_DIGEST,
            "example": {"kind": "whole_snapshot", "snapshot_slug": "nonexistent/snapshot"},
            "critic_model": BUDGET_TEST_MODEL,
            "timeout_seconds": 60,
            "budget_usd": 1.0,
        },
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
    # Snapshot validation happens before image resolution
    mock_registry.resolve_image.assert_not_called()
    mock_registry.start_critic.assert_not_called()


def test_run_critic_returns_critic_run_id(run_critic_client) -> None:
    """POST /api/runs/critic returns critic_run_id immediately (non-blocking)."""
    client, mock_registry = run_critic_client

    expected_run_id = uuid4()
    mock_registry.resolve_image.return_value = FAKE_RESOLVED
    mock_registry.start_critic.return_value = expected_run_id

    response = client.post(
        "/api/runs/critic",
        json={
            "definition_id": FAKE_CRITIC_DIGEST,
            "example": {"kind": "whole_snapshot", "snapshot_slug": "test-fixtures/train1"},
            "critic_model": BUDGET_TEST_MODEL,
            "timeout_seconds": 60,
            "budget_usd": 1.0,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["critic_run_id"] == str(expected_run_id)
    # Non-blocking: no status or container_exit_code in response
    assert "status" not in body
    assert "container_exit_code" not in body


if __name__ == "__main__":
    pytest_bazel.main()
