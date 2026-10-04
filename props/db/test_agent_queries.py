"""Tests for agent query builders and DB views against a synced test database."""

from __future__ import annotations

import pytest
import pytest_bazel

from props.agents.critic_dev.recipes.ground_truth import count_issues_by_snapshot
from props.core.splits import Split
from props.db.database import Database
from props.db.examples import Example
from props.db.models import Snapshot
from props.testing.fixtures.runs import make_fake_critic_run, make_fake_grader_run


@pytest.fixture
def query_test_data(synced_db: Database):
    """Seed critic + grader runs so DB views have non-trivial data."""
    with synced_db.session() as session:
        # Query git fixture examples (snapshots/TPs/FPs already loaded by synced_db)
        # Use explicit join and select columns to avoid lazy loading issues
        train_examples = (
            session.query(Example)
            .join(Snapshot, Example.snapshot_slug == Snapshot.slug)
            .filter(Snapshot.split == Split.TRAIN)
            .limit(2)
            .all()
        )
        valid_examples = (
            session.query(Example)
            .join(Snapshot, Example.snapshot_slug == Snapshot.slug)
            .filter(Snapshot.split == Split.VALID)
            .limit(2)
            .all()
        )

        assert len(train_examples) >= 1, "Need at least 1 train example from git fixtures"
        assert len(valid_examples) >= 2, "Need at least 2 valid examples from git fixtures"

        # Create critic runs using factory (uses attached Example objects directly)
        critic_run_train = make_fake_critic_run(session=session, example=train_examples[0].to_example_spec())
        session.add(critic_run_train)

        critic_run_valid_1 = make_fake_critic_run(session=session, example=valid_examples[0].to_example_spec())
        session.add(critic_run_valid_1)

        critic_run_valid_2 = make_fake_critic_run(session=session, example=valid_examples[1].to_example_spec())
        session.add(critic_run_valid_2)

        session.flush()

        # Create grader runs using factory (one per snapshot)
        grader_run_train = make_fake_grader_run(session=session, snapshot_slug=train_examples[0].snapshot_slug)
        session.add(grader_run_train)

        grader_run_valid_1 = make_fake_grader_run(session=session, snapshot_slug=valid_examples[0].snapshot_slug)
        session.add(grader_run_valid_1)

        grader_run_valid_2 = make_fake_grader_run(session=session, snapshot_slug=valid_examples[1].snapshot_slug)
        session.add(grader_run_valid_2)

        session.flush()
        session.commit()


class TestQueryBuilders:
    """Test query builders execute and return expected data."""

    def test_count_issues_by_snapshot(self, query_test_data, db: Database):
        """count_issues_by_snapshot() returns TP/FP counts per snapshot."""
        with db.session() as session:
            result = session.execute(count_issues_by_snapshot(split=Split.TRAIN)).fetchall()

            # Should have at least 1 train snapshot from git fixtures
            assert len(result) >= 1

            # Check structure - all should be from test-fixtures
            for row in result:
                assert "test-fixtures/" in row.snapshot_slug
                assert row.tp_count >= 0
                assert row.fp_count >= 0
                # tp_count and fp_count should be integers
                assert isinstance(row.tp_count, int)
                assert isinstance(row.fp_count, int)


if __name__ == "__main__":
    pytest_bazel.main()
