"""Integration tests for stats API routes.

Tests the occurrence stats and coverage endpoints with real Postgres
and test fixture data (critic runs + grading edges).
"""

from __future__ import annotations

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from props.backend.auth import get_caller_db
from props.backend.routes import stats
from props.core.models.examples import ExampleSpec
from props.db.database import Database
from props.db.models import AgentRun
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
# gazelle:include_dep //props/testing/fixtures:runs
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db", "props.testing.fixtures.runs")

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


@pytest.fixture
def stats_client(synced_db: Database) -> TestClient:
    """FastAPI TestClient with admin DB wired as agent DB (full access)."""
    app = FastAPI()
    app.include_router(stats.router, prefix="/api/stats")
    app.state.admin_db = synced_db
    # Override agent DB dependency to use admin connection (no RLS restriction).
    # RLS scoping is tested separately in test_split_based_rls.
    app.dependency_overrides[get_caller_db] = lambda: synced_db
    return TestClient(app, raise_server_exceptions=False)


# --- /definitions/{image_digest} ---


def test_definition_detail_not_found(stats_client: TestClient) -> None:
    """Returns 404 for nonexistent digest."""
    resp = stats_client.get("/api/stats/definitions/sha256:" + "f" * 64)
    assert resp.status_code == 404


def test_definition_detail_returns_definition(stats_client: TestClient, synced_db: Database) -> None:
    """Returns definition metadata for a known digest."""
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.commit()

    resp = stats_client.get(f"/api/stats/definitions/{FAKE_CRITIC_DIGEST}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_digest"] == FAKE_CRITIC_DIGEST
    assert body["agent_type"] == "critic"


def test_definition_detail_with_runs(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Returns per-example stats when runs exist for the definition."""
    resp = stats_client.get(f"/api/stats/definitions/{FAKE_CRITIC_DIGEST}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_digest"] == FAKE_CRITIC_DIGEST
    assert body["examples"]


# --- /occurrences ---


def test_occurrences_empty(stats_client: TestClient) -> None:
    """Returns empty list when no runs exist."""
    resp = stats_client.get("/api/stats/occurrences")
    assert resp.status_code == 200
    body = resp.json()
    assert body["occurrences"] == []
    assert body["total"] == 0


def test_occurrences_with_runs(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Returns aggregated occurrence stats after creating critic+grader runs."""
    resp = stats_client.get("/api/stats/occurrences")
    assert resp.status_code == 200
    body = resp.json()
    example, _, _ = test_train_example_with_runs
    rows = [row for row in body["occurrences"] if row["snapshot_slug"] == example.snapshot_slug]
    assert rows
    for row in rows:
        assert row["n_runs"] == 2
        assert row["mean_credit"] == pytest.approx(0.76)
        assert row["min_credit"] == pytest.approx(0.72)
        assert row["max_credit"] == pytest.approx(0.8)


# --- /coverage ---


def test_coverage_empty(stats_client: TestClient) -> None:
    """Returns empty when no runs exist for split."""
    resp = stats_client.get("/api/stats/coverage", params={"split": "valid"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["examples"] == []
    assert body["definitions"] == []
    assert body["cells"] == []
    assert body["max_recall_values"] == []
    assert body["tp_count_values"] == []


def test_coverage_with_runs(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Returns the coverage matrix and distribution histograms for a split with runs."""
    resp = stats_client.get("/api/stats/coverage", params={"split": "train"})
    assert resp.status_code == 200
    body = resp.json()
    example, _, _ = test_train_example_with_runs
    definition_index = next(i for i, row in enumerate(body["definitions"]) if row["image_digest"] == FAKE_CRITIC_DIGEST)
    example_index = next(
        i
        for i, row in enumerate(body["examples"])
        if row["snapshot_slug"] == example.snapshot_slug and row["example_kind"] == str(example.kind)
    )
    assert body["definitions"][definition_index]["evaluated_on_count"] >= 1
    assert body["examples"][example_index]["tp_count"] > 0
    cell = next(
        cell
        for cell in body["cells"]
        if cell["definition_idx"] == definition_index and cell["example_idx"] == example_index
    )
    assert cell["recall"] == pytest.approx(0.76)
    assert cell["is_best"] is True

    assert len(body["max_recall_values"]) > 0
    assert all(0.0 <= v <= 1.0 for v in body["max_recall_values"])
    assert len(body["tp_count_values"]) > 0
    assert all(v > 0 for v in body["tp_count_values"])


if __name__ == "__main__":
    pytest_bazel.main()
