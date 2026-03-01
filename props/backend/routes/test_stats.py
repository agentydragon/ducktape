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
    assert "created_at" in body
    assert "stats" in body
    assert "examples" in body


def test_definition_detail_with_runs(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Returns stats and examples when runs exist for the definition."""
    resp = stats_client.get(f"/api/stats/definitions/{FAKE_CRITIC_DIGEST}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_digest"] == FAKE_CRITIC_DIGEST
    # With runs, we should have stats populated
    assert isinstance(body["stats"], dict)
    assert isinstance(body["examples"], list)


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
    assert body["total"] > 0
    row = body["occurrences"][0]
    assert "tp_id" in row
    assert "occurrence_id" in row
    assert row["n_runs"] >= 1
    assert 0.0 <= row["mean_credit"] <= 1.0


def test_occurrences_filter_by_split(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Filter by split returns only matching rows."""
    resp = stats_client.get("/api/stats/occurrences", params={"split": "train"})
    assert resp.status_code == 200
    for row in resp.json()["occurrences"]:
        assert row["split"] == "train"


def test_occurrences_filter_by_snapshot(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Filter by snapshot_slug returns only matching rows."""
    example, _, _ = test_train_example_with_runs
    slug = example.snapshot_slug
    resp = stats_client.get("/api/stats/occurrences", params={"snapshot_slug": slug})
    assert resp.status_code == 200
    for row in resp.json()["occurrences"]:
        assert row["snapshot_slug"] == slug


def test_occurrences_sort_desc(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """sort_dir=desc returns highest credits first."""
    resp = stats_client.get("/api/stats/occurrences", params={"sort_dir": "desc"})
    assert resp.status_code == 200
    credits = [r["mean_credit"] for r in resp.json()["occurrences"]]
    assert credits == sorted(credits, reverse=True)


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


def test_coverage_distributions_with_runs(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Coverage response includes distribution histograms."""
    resp = stats_client.get("/api/stats/coverage", params={"split": "train"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["max_recall_values"]) > 0
    assert all(0.0 <= v <= 1.0 for v in body["max_recall_values"])
    assert len(body["tp_count_values"]) > 0
    assert all(v > 0 for v in body["tp_count_values"])


def test_coverage_requires_split(stats_client: TestClient) -> None:
    """Split parameter is required."""
    resp = stats_client.get("/api/stats/coverage")
    assert resp.status_code == 422


def test_coverage_with_runs(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """Returns coverage matrix with at least one definition and example."""
    resp = stats_client.get("/api/stats/coverage", params={"split": "train"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["definitions"]) > 0
    assert len(body["examples"]) > 0
    assert len(body["cells"]) > 0

    defn = body["definitions"][0]
    assert "image_digest" in defn
    assert defn["best_on_count"] >= 0
    assert defn["evaluated_on_count"] >= 1

    example = body["examples"][0]
    assert "snapshot_slug" in example
    assert example["tp_count"] > 0

    cell = body["cells"][0]
    assert 0.0 <= cell["recall"] <= 1.0
    assert isinstance(cell["is_best"], bool)


def test_coverage_limit_definitions(
    stats_client: TestClient, test_train_example_with_runs: tuple[ExampleSpec, AgentRun, AgentRun]
) -> None:
    """limit_definitions caps the number of returned definitions."""
    resp = stats_client.get("/api/stats/coverage", params={"split": "train", "limit_definitions": 1})
    assert resp.status_code == 200
    assert len(resp.json()["definitions"]) <= 1


if __name__ == "__main__":
    pytest_bazel.main()
