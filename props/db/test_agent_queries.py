"""Tests for agent query builders against a synced test database."""

import pytest_bazel

from props.agents.critic_dev.recipes.ground_truth import count_issues_by_snapshot
from props.core.splits import Split
from props.db.database import Database

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")


def test_count_issues_by_snapshot(synced_db: Database) -> None:
    with synced_db.session() as session:
        train = {
            row.snapshot_slug: (row.tp_count, row.fp_count)
            for row in session.execute(count_issues_by_snapshot(Split.TRAIN))
        }
        valid = {
            row.snapshot_slug: (row.tp_count, row.fp_count)
            for row in session.execute(count_issues_by_snapshot(Split.VALID))
        }

    assert train["test-fixtures/train1"] == (6, 1)
    assert "test-fixtures/train1" not in valid
    assert "test-fixtures/valid1" in valid
    assert "test-fixtures/valid1" not in train


if __name__ == "__main__":
    pytest_bazel.main()
