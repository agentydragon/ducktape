"""Unit tests for agent SQL query builders.

Tests verify:
1. Query builders execute successfully via SQLAlchemy
2. Return expected data shapes and values

This tests the single source of truth: query_builders.py functions are executed
directly in tests, and the same query builders are compiled to SQL for j2 templates.

Does NOT test:
- RLS policies (covered in test_db_integration.py)
- Docker integration (covered in test_critic_dev_integration.py)
- Database setup/teardown (uses existing db fixture)
"""

from __future__ import annotations

import pytest
import pytest_bazel

from props.core.splits import Split
from props.db import query_builders as qb
from props.db.database import Database
from props.db.examples import Example
from props.db.models import FalsePositive, RecallByDefinitionSplitKind, Snapshot, TruePositive
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import make_fake_critic_run, make_fake_grader_run

pytestmark = [pytest.mark.integration]


@pytest.fixture
def query_test_data(synced_db: Database):
    """Populate database with critic/grader runs for query validation.

    Uses git fixtures for ground truth (Snapshots, TPs, FPs, Examples).
    Creates:
    - 1 prompt
    - 3 critic runs (1 train, 2 valid)
    - 3 grader runs (1 train, 2 valid)
    - Event records (tool_call and function_call_output events)

    Note: Git fixtures provide:
    - test-trivial (TRAIN) - has TPs and examples
    - test-validation (VALID) - has TPs and examples
    - test-validation-2 (VALID) - has TPs and examples
    """
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

    def test_list_train_snapshots(self, query_test_data, db: Database):
        """list_train_snapshots() returns train snapshots in order."""
        with db.session() as session:
            result = session.execute(qb.list_train_snapshots()).fetchall()

            # Should have at least 1 train snapshot from git fixtures
            assert len(result) >= 1

            # Check first row has expected columns and values
            assert "test-fixtures/" in result[0].slug  # Git fixtures use test-fixtures/ prefix
            assert result[0].split == "train"

            # Check ordering (slugs should be sorted)
            slugs = [row.slug for row in result]
            assert slugs == sorted(slugs)

    def test_list_train_true_positives(self, query_test_data, db: Database):
        """list_train_true_positives() returns all TPs for train split."""
        with db.session() as session:
            result = session.execute(qb.list_train_true_positives()).fetchall()

            # Should have at least 1 train true positive from git fixtures
            assert len(result) >= 1

            # Check structure
            assert "test-fixtures/" in result[0].snapshot_slug
            assert result[0].tp_id is not None
            assert result[0].rationale is not None

    def test_list_train_false_positives(self, query_test_data, db: Database):
        """list_train_false_positives() returns all FPs for train split."""
        with db.session() as session:
            result = session.execute(qb.list_train_false_positives()).fetchall()

            # Git fixtures may or may not have FPs - just check structure if any exist
            if len(result) > 0:
                # Check structure
                assert "test-fixtures/" in result[0].snapshot_slug
                assert result[0].fp_id is not None
                assert result[0].rationale is not None

    def test_count_issues_by_snapshot(self, query_test_data, db: Database):
        """count_issues_by_snapshot() returns TP/FP counts per snapshot."""
        with db.session() as session:
            result = session.execute(qb.count_issues_by_snapshot(split=Split.TRAIN)).fetchall()

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

    def test_list_true_positives_for_snapshot(self, query_test_data, db: Database):
        """list_true_positives_for_snapshot() returns TPs for specific snapshot."""
        with db.session() as session:
            # Find a TRAIN snapshot with TPs
            train_snapshot = (
                session.query(Snapshot)
                .filter(Snapshot.split == Split.TRAIN)
                .join(TruePositive, TruePositive.snapshot_slug == Snapshot.slug)
                .first()
            )
            assert train_snapshot, "No TRAIN snapshot with TPs found"

            result = session.execute(qb.list_true_positives_for_snapshot(train_snapshot.slug)).scalars().all()

            # Should have at least 1 TP
            assert len(result) >= 1
            assert result[0].tp_id is not None
            assert result[0].rationale is not None
            assert len(result[0].occurrences) >= 1

    def test_list_false_positives_for_snapshot(self, query_test_data, db: Database):
        """list_false_positives_for_snapshot() returns FPs for specific snapshot."""
        with db.session() as session:
            # Find a TRAIN snapshot with FPs (if any exist)
            train_snapshot_with_fps = (
                session.query(Snapshot)
                .filter(Snapshot.split == Split.TRAIN)
                .join(FalsePositive, FalsePositive.snapshot_slug == Snapshot.slug)
                .first()
            )

            if train_snapshot_with_fps:
                result = (
                    session.execute(qb.list_false_positives_for_snapshot(train_snapshot_with_fps.slug)).scalars().all()
                )
                # Should have at least 1 FP
                assert len(result) >= 1
                assert result[0].fp_id is not None
                assert result[0].rationale is not None
                assert len(result[0].occurrences) >= 1
            else:
                # If no FPs, just verify empty result for any TRAIN snapshot
                train_snapshot = session.query(Snapshot).filter(Snapshot.split == Split.TRAIN).first()
                assert train_snapshot, "No TRAIN snapshot found"
                result = session.execute(qb.list_false_positives_for_snapshot(train_snapshot.slug)).scalars().all()
                assert len(result) == 0

    def test_valid_aggregates_view(self, query_test_data, db: Database):
        """aggregated_recall_by_definition view computes statistics for valid split."""

        with db.session() as session:
            # Query the aggregated_recall_by_definition view for valid split
            result = (
                session.query(RecallByDefinitionSplitKind)
                .filter(RecallByDefinitionSplitKind.split == Split.VALID)
                .all()
            )

            # Should have at least 1 row (from valid grader runs created in fixture)
            assert len(result) >= 1

            # Check first row has expected structure (occurrence-based metrics)
            row = result[0]
            # Check occurrence stats are present (StatsWithCI type)
            if row.credit_stats is not None:
                assert row.credit_stats.mean >= 0.0
            assert row.recall_denominator >= 0
            # Check status counts are present (dict from JSONB) with non-negative values
            assert row.status_counts is not None
            assert all(count >= 0 for count in row.status_counts.values())

    def test_critic_runs_for_snapshot(self, query_test_data, db: Database):
        """critic_runs_for_snapshot() returns critic runs for a specific snapshot."""
        with db.session() as session:
            # Find a TRAIN file-set example (with files_hash) that has critic runs
            train_example = (
                session.query(Example)
                .join(Snapshot, Example.snapshot_slug == Snapshot.slug)
                .filter(Snapshot.split == Split.TRAIN)
                .filter(Example.files_hash.isnot(None))  # File-set example only
                .first()
            )
            assert train_example, "No TRAIN file-set example found"

            result = session.execute(qb.critic_runs_for_snapshot(train_example.snapshot_slug, limit=5)).fetchall()

            # Should have at least 1 critic run (created in query_test_data fixture)
            assert len(result) >= 1

            # Check structure (uses agent_runs table now, not legacy critic_runs)
            row = result[0]
            assert row.agent_run_id is not None  # Primary key is agent_run_id now
            assert row.status is not None  # AgentRunStatus enum value
            assert row.created_at is not None
            # files_hash may be None for whole-snapshot examples, or a string for file-set examples
            assert row.model == DEFAULT_TEST_MODEL


if __name__ == "__main__":
    pytest_bazel.main()
