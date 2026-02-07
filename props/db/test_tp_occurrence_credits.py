"""Test tp_occurrence_credits view: runs without grading edges get zero credit."""

import pytest_bazel
from sqlalchemy import text
from sqlalchemy.orm import Session

from props.core.models.examples import ExampleKind, SingleFileSetExample
from props.core.splits import Split
from props.db.examples import Example
from props.db.models import AgentRunStatus, GradingEdge, RecallByDefinitionSplitKind, RecallByExample
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import (
    FAKE_CRITIC_DIGEST,
    make_fake_critic_run,
    make_fake_grader_run,
    make_fake_grader_run_with_credit,
    make_reported_issues,
)


def test_ungraded_run_appears_with_zero_credit(synced_test_session: Session, example_subtract_orm: Example):
    """Runs without grading edges get zero credit via COALESCE."""
    critic_run = make_fake_critic_run(
        session=synced_test_session,
        example=example_subtract_orm.to_example_spec(),
        model="test-critic-model",
        status=AgentRunStatus.TIMED_OUT,
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    result = synced_test_session.execute(
        text("""
            SELECT critic_run_id, tp_id, occurrence_id, found_credit
            FROM tp_occurrence_credits WHERE critic_run_id = :run_id
        """),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchone()

    assert result is not None, "Ungraded run should still generate a row"
    assert result.critic_run_id == critic_run.agent_run_id
    assert result.tp_id == "tp-001"
    assert result.found_credit == 0.0


def test_only_expected_occurrences_included(synced_test_session: Session, example_subtract_orm: Example):
    """Rows are only generated for occurrences in expected recall scope."""
    critic_run = make_fake_critic_run(
        session=synced_test_session,
        example=example_subtract_orm.to_example_spec(),
        model="test-critic-model",
        status=AgentRunStatus.TIMED_OUT,
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    results = synced_test_session.execute(
        text("SELECT tp_id, occurrence_id FROM tp_occurrence_credits WHERE critic_run_id = :run_id ORDER BY tp_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchall()

    # subtract.py example has 1 TP in expected recall scope
    assert len(results) == 1, "Should only include occurrence in expected recall scope"
    assert results[0].tp_id == "tp-001"


def test_whole_snapshot_includes_all_occurrences(synced_test_session: Session, test_snapshot):
    """Whole-snapshot runs get rows for all occurrences in the snapshot."""
    example = (
        synced_test_session.query(Example)
        .filter_by(snapshot_slug=test_snapshot, example_kind=ExampleKind.WHOLE_SNAPSHOT)
        .one()
    )

    critic_run = make_fake_critic_run(
        session=synced_test_session,
        example=example.to_example_spec(),
        model="test-critic-model",
        status=AgentRunStatus.TIMED_OUT,
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    results = synced_test_session.execute(
        text("SELECT tp_id FROM tp_occurrence_credits WHERE critic_run_id = :run_id ORDER BY tp_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchall()

    # train1 has 5 TPs
    assert len(results) == 5, f"Should include all 5 occurrences, got {len(results)}"
    tp_ids = {r.tp_id for r in results}
    assert tp_ids == {"tp-001", "tp-002", "tp-003", "tp-004", "tp-005"}


def test_graded_run_gets_actual_credit(
    synced_test_session: Session, example_subtract_orm: Example, tp_occurrence_single: tuple[str, str]
):
    """Runs with grading edges get the actual credit from those edges."""
    critic_run = make_fake_critic_run(
        session=synced_test_session,
        example=example_subtract_orm.to_example_spec(),
        model="test-critic-model",
        status=AgentRunStatus.EXITED,
    )
    synced_test_session.add(critic_run)
    synced_test_session.flush()

    tp_id, occ_id = tp_occurrence_single
    make_reported_issues(
        agent_run_id=critic_run.agent_run_id,
        issue_ids=["input-1"],
        session=synced_test_session,
        location_file="subtract.py",
    )

    grader_run = make_fake_grader_run(
        session=synced_test_session, snapshot_slug=example_subtract_orm.snapshot_slug, model="test-grader-model"
    )
    synced_test_session.add(grader_run)
    synced_test_session.flush()

    edge = GradingEdge(
        critique_run_id=critic_run.agent_run_id,
        critique_issue_id="input-1",
        snapshot_slug=example_subtract_orm.snapshot_slug,
        tp_id=tp_id,
        tp_occurrence_id=occ_id,
        fp_id=None,
        fp_occurrence_id=None,
        credit=0.8,
        rationale="Partially found",
        grader_run_id=grader_run.agent_run_id,
    )
    synced_test_session.add(edge)
    synced_test_session.commit()

    result = synced_test_session.execute(
        text("SELECT found_credit FROM tp_occurrence_credits WHERE critic_run_id = :run_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchone()

    assert result is not None
    assert result.found_credit == 0.8


def test_multiple_occurrences_with_or_logic(synced_test_session: Session, example_multi_tp_orm: Example):
    """Test catchability with OR logic in critic_scopes_expected_to_recall."""
    critic_run = make_fake_critic_run(
        session=synced_test_session,
        example=example_multi_tp_orm.to_example_spec(),
        model="test-critic-model",
        status=AgentRunStatus.TIMED_OUT,
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    results = synced_test_session.execute(
        text("SELECT tp_id FROM tp_occurrence_credits WHERE critic_run_id = :run_id ORDER BY tp_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchall()

    # multi-TP example has 2 occurrences in expected recall scope
    assert len(results) == 2, f"Expected 2 occurrences in recall scope, got {len(results)}"


def test_multiple_grader_runs_do_not_overweight_critic_run(
    synced_test_session: Session, example_subtract_orm: Example, tp_occurrence_single: tuple[str, str]
):
    """Test that multiple grader runs for same critic run don't cause overweighting."""
    # Ungraded run (timed out, no grading edges → 0 credit)
    ungraded_run = make_fake_critic_run(
        session=synced_test_session, example=example_subtract_orm.to_example_spec(), status=AgentRunStatus.TIMED_OUT
    )
    synced_test_session.add(ungraded_run)

    # Graded run with 3 grader runs at different credits
    graded_run = make_fake_critic_run(
        session=synced_test_session,
        example=example_subtract_orm.to_example_spec(),
        model=DEFAULT_TEST_MODEL,
        status=AgentRunStatus.EXITED,
    )
    synced_test_session.add(graded_run)
    synced_test_session.flush()

    for idx, credit in enumerate([0.2, 0.3, 0.4]):
        make_fake_grader_run_with_credit(
            session=synced_test_session,
            critic_run=graded_run,
            tp_occurrence=tp_occurrence_single,
            credit=credit,
            input_idx=idx,
        )

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByDefinitionSplitKind)
        .filter_by(critic_image_digest=FAKE_CRITIC_DIGEST, split=Split.TRAIN, critic_model=DEFAULT_TEST_MODEL)
        .one()
    )

    assert sum(result.status_counts.values()) == 2, "Should count both critic runs"
    assert result.status_counts[AgentRunStatus.EXITED] == 1
    assert result.status_counts[AgentRunStatus.TIMED_OUT] == 1

    # SUM credits per critique: 0.2+0.3+0.4=0.9. Mean across 2 runs: (0.0 + 0.9) / 2 = 0.45
    avg_caught = result.credit_stats.mean if result.credit_stats else 0.0
    assert abs(avg_caught - 0.45) < 0.01, f"credit_stats.mean should be 0.45, got {avg_caught}"
    assert result.recall_denominator == 1


def test_aggregated_view_counts_by_status(synced_test_session: Session, example_subtract_orm: Example):
    """Aggregated view correctly counts runs by status."""
    # Create 3 successful runs with grader runs
    for _ in range(3):
        critic_run = make_fake_critic_run(
            session=synced_test_session, example=example_subtract_orm.to_example_spec(), status=AgentRunStatus.EXITED
        )
        synced_test_session.add(critic_run)
        synced_test_session.flush()
        grader_run = make_fake_grader_run(
            session=synced_test_session, snapshot_slug=example_subtract_orm.snapshot_slug, model="test-grader-model"
        )
        synced_test_session.add(grader_run)

    # Create 2 timed_out failures
    for _ in range(2):
        synced_test_session.add(
            make_fake_critic_run(
                session=synced_test_session,
                example=example_subtract_orm.to_example_spec(),
                status=AgentRunStatus.TIMED_OUT,
            )
        )

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByDefinitionSplitKind)
        .filter_by(critic_image_digest=FAKE_CRITIC_DIGEST, split=Split.TRAIN, critic_model=DEFAULT_TEST_MODEL)
        .one()
    )

    assert sum(result.status_counts.values()) == 5
    assert result.status_counts[AgentRunStatus.EXITED] == 3
    assert result.status_counts[AgentRunStatus.TIMED_OUT] == 2


def test_aggregated_view_status_counts_all_exited(synced_test_session: Session, example_subtract_orm: Example):
    """When all runs exit, timed_out count is zero."""
    for _ in range(3):
        critic_run = make_fake_critic_run(
            session=synced_test_session, example=example_subtract_orm.to_example_spec(), status=AgentRunStatus.EXITED
        )
        synced_test_session.add(critic_run)
        synced_test_session.flush()
        grader_run = make_fake_grader_run(
            session=synced_test_session, snapshot_slug=example_subtract_orm.snapshot_slug, model="test-grader-model"
        )
        synced_test_session.add(grader_run)

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByDefinitionSplitKind)
        .filter_by(critic_image_digest=FAKE_CRITIC_DIGEST, split=Split.TRAIN, critic_model=DEFAULT_TEST_MODEL)
        .one()
    )

    assert sum(result.status_counts.values()) == 3
    assert result.status_counts[AgentRunStatus.EXITED] == 3
    assert result.status_counts.get(AgentRunStatus.TIMED_OUT, 0) == 0


def test_aggregated_recall_by_example_has_correct_weighting(
    synced_test_session: Session, example_subtract_orm: Example, tp_occurrence_single: tuple[str, str]
):
    """Test that aggregated_recall_by_example correctly weights critic runs."""
    # Ungraded run (timed out, no grading edges → 0 credit)
    ungraded_run = make_fake_critic_run(
        session=synced_test_session, example=example_subtract_orm.to_example_spec(), status=AgentRunStatus.TIMED_OUT
    )
    synced_test_session.add(ungraded_run)

    # Graded run with 3 grader runs at different credits
    graded_run = make_fake_critic_run(
        session=synced_test_session,
        example=example_subtract_orm.to_example_spec(),
        model=DEFAULT_TEST_MODEL,
        status=AgentRunStatus.EXITED,
    )
    synced_test_session.add(graded_run)
    synced_test_session.flush()

    for idx, credit in enumerate([0.1, 0.2, 0.3]):
        make_fake_grader_run_with_credit(
            session=synced_test_session,
            critic_run=graded_run,
            tp_occurrence=tp_occurrence_single,
            credit=credit,
            input_idx=idx,
        )

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByExample)
        .filter_by(
            snapshot_slug=example_subtract_orm.snapshot_slug,
            example_kind=example_subtract_orm.example_kind,
            files_hash=example_subtract_orm.files_hash,
        )
        .one()
    )

    assert sum(result.status_counts.values()) == 2
    assert result.status_counts[AgentRunStatus.EXITED] == 1
    assert result.status_counts[AgentRunStatus.TIMED_OUT] == 1

    # SUM credits per critique: 0.1+0.2+0.3=0.6. Mean across 2 runs: (0.0 + 0.6) / 2 = 0.3
    avg_caught = result.credit_stats.mean if result.credit_stats else 0.0
    assert abs(avg_caught - 0.3) < 0.01, f"Expected 0.3, got {avg_caught}"
    assert result.recall_denominator == 1


def test_occurrence_statistics_has_correct_n_critic_runs(
    synced_test_session: Session, subtract_file_example: SingleFileSetExample, tp_occurrence_single: tuple[str, str]
):
    """Test that aggregated_recall_by_example counts critic runs correctly."""
    example = Example.from_spec(synced_test_session, subtract_file_example)

    # Critic run 1: graded 1 time (credit 0.8)
    run1 = make_fake_critic_run(
        session=synced_test_session,
        example=subtract_file_example,
        model=DEFAULT_TEST_MODEL,
        status=AgentRunStatus.EXITED,
    )
    synced_test_session.add(run1)
    synced_test_session.flush()
    make_fake_grader_run_with_credit(
        session=synced_test_session, critic_run=run1, tp_occurrence=tp_occurrence_single, credit=0.8, input_idx=0
    )

    # Critic run 2: graded 4 times (credits sum to 0.7, within check_edge_credit_sum <= 1.0)
    run2 = make_fake_critic_run(
        session=synced_test_session,
        example=subtract_file_example,
        model=DEFAULT_TEST_MODEL,
        status=AgentRunStatus.EXITED,
    )
    synced_test_session.add(run2)
    synced_test_session.flush()
    for idx, credit in enumerate([0.1, 0.15, 0.2, 0.25]):
        make_fake_grader_run_with_credit(
            session=synced_test_session,
            critic_run=run2,
            tp_occurrence=tp_occurrence_single,
            credit=credit,
            input_idx=idx,
        )

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByExample)
        .filter_by(
            snapshot_slug=example.snapshot_slug, example_kind=example.example_kind, files_hash=example.files_hash
        )
        .one()
    )

    # Should count 2 critic runs, not 5 grader runs
    assert sum(result.status_counts.values()) == 2
    assert result.status_counts[AgentRunStatus.EXITED] == 2

    # Run 1: SUM=0.8, Run 2: SUM(0.1+0.15+0.2+0.25)=0.7 -> mean = (0.8+0.7)/2 = 0.75
    avg_caught = result.credit_stats.mean if result.credit_stats else 0.0
    assert abs(avg_caught - 0.75) < 0.01, f"Expected 0.75, got {avg_caught}"


if __name__ == "__main__":
    pytest_bazel.main()
