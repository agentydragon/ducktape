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

from props.backend.auth import AdminRole, AuthenticatedIdentity, get_caller_db, require_critic_run_access
from props.backend.deps import get_admin_db
from props.backend.routes import runs
from props.core.agent_types import CriticTypeConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentRunBudgetStatus, AgentRunStatus, LLMRequest
from props.orchestration.agent_registry import BudgetExceededError, ImageResolutionError, ResolvedImage
from props.testing.constants import BUDGET_TEST_MODEL, TRAIN_EXAMPLE
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions, make_fake_critic_run

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

FAKE_RESOLVED = ResolvedImage(digest=FAKE_CRITIC_DIGEST, oci_ref=f"localhost:5000/critic@{FAKE_CRITIC_DIGEST}")


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


def test_active_runs_returns_only_active_run_details(synced_db: Database, parent_run: UUID) -> None:
    with synced_db.session() as session:
        finished = make_fake_critic_run(session=session, example=TRAIN_EXAMPLE, status=AgentRunStatus.EXITED)
        finished_id = finished.agent_run_id
        session.add(finished)
        session.commit()

    app = FastAPI()
    app.include_router(runs.router, prefix="/api/runs")
    app.dependency_overrides[get_caller_db] = lambda: synced_db
    with TestClient(app) as client:
        response = client.get("/api/runs/active")

    assert response.status_code == 200
    rows = {row["agent_run_id"]: row for row in response.json()["runs"]}
    assert str(finished_id) not in rows
    active = rows[str(parent_run)]
    assert active["image_digest"] == FAKE_CRITIC_DIGEST
    assert active["model"] == BUDGET_TEST_MODEL
    assert active["status"] == "in_progress"
    assert active["created_at"]


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
    app.dependency_overrides[require_critic_run_access] = lambda: AuthenticatedIdentity(
        username="admin", password="admin", role=AdminRole()
    )
    app.state.registry = mock_registry

    client = TestClient(app, raise_server_exceptions=False)
    return client, mock_registry


def _critic_request(
    *, definition_id: str = FAKE_CRITIC_DIGEST, snapshot_slug: str = "test-fixtures/train1", budget_usd: float = 1.0
) -> dict[str, object]:
    return {
        "definition_id": definition_id,
        "example": {"kind": "whole_snapshot", "snapshot_slug": snapshot_slug},
        "critic_model": BUDGET_TEST_MODEL,
        "timeout_seconds": 60,
        "budget_usd": budget_usd,
    }


def test_run_critic_budget_exceeded_returns_422(run_critic_client) -> None:
    """POST /api/runs/critic returns 422 when budget exceeds parent's remaining."""
    client, mock_registry = run_critic_client

    mock_registry.resolve_image.return_value = FAKE_RESOLVED
    mock_registry.start_critic.side_effect = BudgetExceededError(
        "Cannot spawn child with $5.00 budget: parent has $0.75 remaining ($1.25 spent of $2.00)"
    )

    response = client.post("/api/runs/critic", json=_critic_request(budget_usd=5.0))

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

    response = client.post("/api/runs/critic", json=_critic_request(definition_id="nonexistent"))

    assert response.status_code == 422
    assert "Image not found" in response.json()["detail"]


def test_run_critic_registry_initializing_returns_503(run_critic_client) -> None:
    """POST /api/runs/critic returns 503 while backend slow-startup is initializing registry."""
    client, _ = run_critic_client
    client.app.state.registry = None

    response = client.post("/api/runs/critic", json=_critic_request())

    assert response.status_code == 503


def test_run_critic_uses_registry_dependency_override(run_critic_client) -> None:
    client, _ = run_critic_client
    client.app.state.registry = None
    registry = AsyncMock()
    registry.resolve_image.side_effect = ImageResolutionError("override registry")
    client.app.dependency_overrides[runs.get_registry] = lambda: registry

    response = client.post("/api/runs/critic", json=_critic_request())

    assert response.status_code == 422
    assert response.json()["detail"] == "override registry"
    registry.resolve_image.assert_awaited_once()


def test_run_critic_snapshot_not_found_returns_404(run_critic_client) -> None:
    """POST /api/runs/critic returns 404 when snapshot doesn't exist."""
    client, mock_registry = run_critic_client

    response = client.post("/api/runs/critic", json=_critic_request(snapshot_slug="nonexistent/snapshot"))

    assert response.status_code == 404
    # Snapshot validation happens before image resolution
    mock_registry.resolve_image.assert_not_called()
    mock_registry.start_critic.assert_not_called()


if __name__ == "__main__":
    pytest_bazel.main()
