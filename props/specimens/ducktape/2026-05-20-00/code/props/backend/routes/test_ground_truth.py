"""Integration tests for ground truth API routes.

Verifies that /api/gt/* endpoints work via RLS-based access control:
- Admin/evaluator: sees all snapshots and issues
- critic_dev_optimize agent: sees only TRAIN split ground truth
- Unauthenticated: 401
"""

from __future__ import annotations

from textwrap import dedent

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from props.backend.auth import get_caller_db
from props.backend.routes import ground_truth
from props.core.agent_types import CriticDevOptimizeTypeConfig, TargetMetric
from props.core.models.examples import SingleFileSetExample
from props.db.database import Database
from props.db.models import TruePositive
from props.testing.fixtures.credentials import make_agent_credentials
from props.testing.fixtures.runs import FAKE_CRITIC_DEV_OPTIMIZE_DIGEST


def make_gt_client(caller_db: Database) -> TestClient:
    """Build a TestClient for the ground truth router using caller_db for all queries."""
    app = FastAPI()
    app.include_router(ground_truth.router, prefix="/api/gt")
    app.dependency_overrides[get_caller_db] = lambda: caller_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
async def critic_dev_optimize_gt_client(synced_db: Database):
    creds = await make_agent_credentials(
        synced_db,
        CriticDevOptimizeTypeConfig(
            target_metric=TargetMetric.TARGETED, optimizer_model="gpt-4o-mini", critic_model="gpt-4o-mini"
        ),
        FAKE_CRITIC_DEV_OPTIMIZE_DIGEST,
    )
    user_db = Database.per_request(synced_db.config.with_user(creds.username, creds.password))
    try:
        yield make_gt_client(user_db)
    finally:
        user_db.dispose()


# --- Admin access ---


def test_admin_can_list_snapshots(synced_db: Database) -> None:
    """Admin (full access, bypasses RLS) sees all snapshots."""
    resp = make_gt_client(synced_db).get("/api/gt/snapshots")
    assert resp.status_code == 200
    slugs = {s["slug"] for s in resp.json()["snapshots"]}
    # synced_db has test-fixtures/train1 (train) and test-fixtures/valid1 (valid)
    assert "test-fixtures/train1" in slugs
    assert "test-fixtures/valid1" in slugs


def test_admin_can_get_snapshot_detail(synced_db: Database) -> None:
    """Admin can retrieve full TP/FP detail for any snapshot."""
    resp = make_gt_client(synced_db).get("/api/gt/snapshots/test-fixtures/train1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "test-fixtures/train1"
    assert body["split"] == "train"
    # train1 specimen has tp-001 through tp-006
    assert len(body["true_positives"]) > 0


# --- critic_dev_optimize agent: RLS-filtered access ---


async def test_critic_dev_optimize_sees_all_snapshot_metadata(critic_dev_optimize_gt_client: TestClient) -> None:
    """critic_dev_optimize can list snapshots (all agents see snapshot metadata).

    The snapshots_agent_select RLS policy allows any active agent run to read
    snapshot metadata (slug, split). Sensitive data access is enforced per-table.
    """
    resp = critic_dev_optimize_gt_client.get("/api/gt/snapshots")
    assert resp.status_code == 200
    slugs = {s["slug"] for s in resp.json()["snapshots"]}
    # All snapshot metadata is visible regardless of split
    assert "test-fixtures/train1" in slugs
    assert "test-fixtures/valid1" in slugs


async def test_critic_dev_optimize_sees_train_tps_in_detail(
    synced_db: Database, critic_dev_optimize_gt_client: TestClient
) -> None:
    """critic_dev_optimize sees TPs on train split snapshot.

    can_access_snapshot_ground_truth() permits critic_dev_optimize access
    to TRAIN split ground truth to enable optimization.
    """
    with synced_db.session() as session:
        train_tp_ids = {
            tp.tp_id for tp in session.query(TruePositive).filter_by(snapshot_slug="test-fixtures/train1").all()
        }
    assert len(train_tp_ids) > 0, "train1 specimen must have TPs (check specimen sync)"

    resp = critic_dev_optimize_gt_client.get("/api/gt/snapshots/test-fixtures/train1")
    assert resp.status_code == 200
    body = resp.json()
    returned_tp_ids = {tp["tp_id"] for tp in body["true_positives"]}
    assert returned_tp_ids == train_tp_ids, dedent(f"""
        critic_dev_optimize must see all TRAIN split TPs via RLS.
        Expected: {train_tp_ids}
        Got:      {returned_tp_ids}
    """)


async def test_critic_dev_optimize_cannot_see_valid_tps_in_detail(
    synced_db: Database, critic_dev_optimize_gt_client: TestClient
) -> None:
    """critic_dev_optimize cannot see TPs on valid/test snapshots (RLS blocks).

    can_access_snapshot_ground_truth() returns false for non-train snapshots
    for critic_dev_optimize, preventing overfitting to validation data.
    """
    with synced_db.session() as session:
        valid_tp_count = session.query(TruePositive).filter_by(snapshot_slug="test-fixtures/valid1").count()
    assert valid_tp_count > 0, "valid1 specimen must have TPs (check specimen sync)"

    resp = critic_dev_optimize_gt_client.get("/api/gt/snapshots/test-fixtures/valid1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["true_positives"] == [], dedent("""
        critic_dev_optimize must NOT see valid split TPs.
        RLS policy can_access_snapshot_ground_truth() must block access
        for non-train snapshots to prevent overfitting.
    """)


def test_admin_snapshot_detail_with_whole_snapshot_scope(synced_db: Database) -> None:
    """Exercises the CAST(:kind AS example_kind_enum) SQL path.

    Without the CAST, SQLAlchemy interprets :kind::example_kind_enum as a bind
    parameter :kind followed by invalid SQL, causing a SyntaxError at query time.
    Also exercises is_fp_relevant_for_scope which must use normalized tables.
    """
    resp = make_gt_client(synced_db).get(
        "/api/gt/snapshots/test-fixtures/train1", params={"example_kind": "whole_snapshot"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "test-fixtures/train1"
    assert len(body["true_positives"]) > 0


def test_admin_snapshot_detail_with_file_set_scope(
    synced_db: Database, subtract_file_example: SingleFileSetExample
) -> None:
    """Exercises scope filtering with file_set example_kind and files_hash.

    Uses the subtract.py file-set from train1 git fixtures. Exercises the
    file_set branch of is_tp_in_expected_recall_scope and is_fp_relevant_for_scope.
    """
    resp = make_gt_client(synced_db).get(
        f"/api/gt/snapshots/{subtract_file_example.snapshot_slug}",
        params={"example_kind": "file_set", "files_hash": subtract_file_example.files_hash},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == str(subtract_file_example.snapshot_slug)


if __name__ == "__main__":
    pytest_bazel.main()
