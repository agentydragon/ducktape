"""Test assertion helpers for props tests."""

from __future__ import annotations

from props.agents.grader.drift_handler import get_drift
from props.db.database import Database


def assert_no_pending(snapshot_slug: str, db: Database) -> None:
    """Assert no pending grading or clustering work remains for the snapshot.

    Raises AssertionError with details of what's pending if assertion fails.
    """
    drift = get_drift(snapshot_slug, db)
    if drift.grading or drift.clustering:
        raise AssertionError(f"Expected no pending work for {snapshot_slug}. drift={drift}")
