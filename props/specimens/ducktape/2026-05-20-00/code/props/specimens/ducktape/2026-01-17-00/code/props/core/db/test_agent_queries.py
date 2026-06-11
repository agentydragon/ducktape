"""Unit tests for agent SQL query builders.

Tests verify:
1. Query builders execute successfully via SQLAlchemy
2. Return expected data shapes and values

This tests the single source of truth: query_builders.py functions are executed
directly in tests, and the same query builders are compiled to SQL for j2 templates.

Does NOT test:
- RLS policies (covered in test_db_integration.py)
- Docker integration (covered in test_prompt_optimizer_integration.py)
- Database setup/teardown (uses existing test_db fixture)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from props.core.db import query_builders as qb
from props.core.db.examples import Example
from props.core.db.models import Event, FalsePositive, RecallByDefinitionSplitKind, Snapshot, TruePositive
from props.core.db.session import get_session
from props.core.splits import Split
from props.testing.fixtures import make_critic_run, make_grader_run

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def query_test_data(synced_test_db):
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
    with get_session() as session:
        # Query git fixture examples (snapshots/TPs/FPs already loaded by synced_test_db)
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
        critic_run_train = make_critic_run(
            example=train_examples[0], completion_summary="Test completion summary for train"
        )
        session.add(critic_run_train)

        critic_run_valid_1 = make_critic_run(
            example=valid_examples[0], completion_summary="Test completion summary for valid-1"
        )
        session.add(critic_run_valid_1)

        critic_run_valid_2 = make_critic_run(
            example=valid_examples[1], completion_summary="Test completion summary for valid-2"
        )
        session.add(critic_run_valid_2)

        session.flush()

        # Create grader runs using factory
        grader_run_train = make_grader_run(critic_run=critic_run_train)
        session.add(grader_run_train)

        grader_run_valid_1 = make_grader_run(critic_run=critic_run_valid_1)
        session.add(grader_run_valid_1)

        grader_run_valid_2 = make_grader_run(critic_run=critic_run_valid_2)
        session.add(grader_run_valid_2)

        session.flush()

        # Create event records for one agent_run_id (for event query tests)
        agent_run_id = critic_run_train.agent_run_id
        now = datetime.now()
        event_specs = [
            (0, "tool_call", {"name": "Read", "args_json": '{"file_path": "test.py"}', "call_id": "call-1"}),
            (1, "function_call_output", {"call_id": "call-1", "result": {"isError": False}}),
            (2, "tool_call", {"name": "Grep", "args_json": '{"pattern": "foo"}', "call_id": "call-2"}),
            (3, "function_call_output", {"call_id": "call-2", "result": {"isError": True}}),
        ]
        for seq_num, evt_type, payload in event_specs:
            session.add(
                Event(
                    agent_run_id=agent_run_id, sequence_num=seq_num, event_type=evt_type, timestamp=now, payload=payload
                )
            )

        session.commit()
        return agent_run_id  # Return for use in event query tests


class TestQueryBuilders:
    """Test query builders execute and return expected data."""

    def test_list_train_snapshots(self, query_test_data):
        """list_train_snapshots() returns train snapshots in order."""
        with get_session() as session:
            result = session.execute(qb.list_train_snapshots()).fetchall()

            # Should have at least 1 train snapshot from git fixtures
            assert len(result) >= 1

            # Check first row has expected columns and values
            assert "test-fixtures/" in result[0].slug  # Git fixtures use test-fixtures/ prefix
            assert result[0].split == "train"

            # Check ordering (slugs should be sorted)
            slugs = [row.slug for row in result]
            assert slugs == sorted(slugs)

    def test_list_train_true_positives(self, query_test_data):
        """list_train_true_positives() returns all TPs for train split."""
        with get_session() as session:
            result = session.execute(qb.list_train_true_positives()).fetchall()

            # Should have at least 1 train true positive from git fixtures
            assert len(result) >= 1

            # Check structure
            assert "test-fixtures/" in result[0].snapshot_slug
            assert result[0].tp_id is not None
            assert result[0].rationale is not None

    def test_list_train_false_positives(self, query_test_data):
        """list_train_false_positives() returns all FPs for train split."""
        with get_session() as session:
            result = session.execute(qb.list_train_false_positives()).fetchall()

            # Git fixtures may or may not have FPs - just check structure if any exist
            if len(result) > 0:
                # Check structure
                assert "test-fixtures/" in result[0].snapshot_slug
                assert result[0].fp_id is not None
                assert result[0].rationale is not None

    def test_count_issues_by_snapshot(self, query_test_data):
        """count_issues_by_snapshot() returns TP/FP counts per snapshot."""
        with get_session() as session:
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

    def test_list_true_positives_for_snapshot(self, query_test_data):
        """list_true_positives_for_snapshot() returns TPs for specific snapshot."""
        with get_session() as session:
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

    def test_list_false_positives_for_snapshot(self, query_test_data):
        """list_false_positives_for_snapshot() returns FPs for specific snapshot."""
        with get_session() as session:
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

    def test_valid_aggregates_view(self, query_test_data):
        """aggregated_recall_by_definition view computes statistics for valid split."""

        with get_session() as session:
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

    def test_critic_runs_for_snapshot(self, query_test_data):
        """critic_runs_for_snapshot() returns critic runs for a specific snapshot."""
        with get_session() as session:
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
            assert row.model == "test-model"


class TestJsonbNullFiltering:
    """Test that queries properly filter out JSONB null values.

    JSONB null is different from SQL NULL:
    - SQL NULL: column value is not present (output IS NULL) - NOT possible in schema
    - JSONB null: column contains the JSON literal `null` (output = 'null'::jsonb)

    The database schema has output NOT NULL, so only JSONB null values are possible.
    Queries must filter out JSONB null values to avoid null metrics in results.

    Note: We use raw SQL to insert test data to precisely control JSONB content.
    """
