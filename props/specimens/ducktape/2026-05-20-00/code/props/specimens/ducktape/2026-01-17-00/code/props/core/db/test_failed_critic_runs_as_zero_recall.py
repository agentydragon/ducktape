"""Test that failed critic runs appear in occurrence_credits view with zero credit."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.examples import Example
from props.core.db.models import AgentRunStatus, GradingEdge, RecallByDefinitionSplitKind, RecallByExample
from props.core.models.examples import ExampleKind, SingleFileSetExample
from props.core.splits import Split
from props.testing.fixtures import (
    EMPTY_CANONICAL_ISSUES_SNAPSHOT,
    make_critic_run,
    make_grader_run,
    make_grader_run_with_credit,
    make_reported_issues,
)


def test_failed_critic_run_appears_with_zero_credit(synced_test_session: Session, example_subtract_orm: Example):
    """Test that max_turns_exceeded critic runs generate zero-credit rows."""
    critic_run = make_critic_run(
        example=example_subtract_orm, model="test-critic-model", status=AgentRunStatus.MAX_TURNS_EXCEEDED
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    result = synced_test_session.execute(
        text("""
            SELECT critic_run_id, tp_id, occurrence_id, found_credit, grader_rationale
            FROM occurrence_credits WHERE critic_run_id = :run_id
        """),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchone()

    assert result is not None, "Failed critic run should generate zero-credit row"
    assert result.critic_run_id == critic_run.agent_run_id
    assert result.tp_id == "tp-001"
    assert result.found_credit == 0.0
    assert "max_turns_exceeded" in result.grader_rationale


def test_context_length_exceeded_also_counted_as_zero(synced_test_session: Session, example_subtract_orm: Example):
    """Test that context_length_exceeded critic runs also generate zero-credit rows."""
    critic_run = make_critic_run(
        example=example_subtract_orm, model="test-critic-model", status=AgentRunStatus.CONTEXT_LENGTH_EXCEEDED
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    result = synced_test_session.execute(
        text("SELECT found_credit, grader_rationale FROM occurrence_credits WHERE critic_run_id = :run_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchone()

    assert result is not None
    assert result.found_credit == 0.0
    assert "context_length_exceeded" in result.grader_rationale


def test_only_expected_occurrences_included_for_failures(synced_test_session: Session, example_subtract_orm: Example):
    """Test that failed runs only generate zero-credit rows for occurrences in expected recall scope."""
    critic_run = make_critic_run(
        example=example_subtract_orm, model="test-critic-model", status=AgentRunStatus.MAX_TURNS_EXCEEDED
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    results = synced_test_session.execute(
        text("SELECT tp_id, occurrence_id FROM occurrence_credits WHERE critic_run_id = :run_id ORDER BY tp_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchall()

    # subtract.py example has 1 TP in expected recall scope
    assert len(results) == 1, "Should only include occurrence in expected recall scope"
    assert results[0].tp_id == "tp-001"


def test_whole_snapshot_failure_includes_all_occurrences(synced_test_session: Session, test_snapshot):
    """Test that whole-snapshot failed runs include all occurrences."""
    example = (
        synced_test_session.query(Example)
        .filter_by(snapshot_slug=test_snapshot, example_kind=ExampleKind.WHOLE_SNAPSHOT)
        .one()
    )

    critic_run = make_critic_run(example=example, model="test-critic-model", status=AgentRunStatus.MAX_TURNS_EXCEEDED)
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    results = synced_test_session.execute(
        text("SELECT tp_id FROM occurrence_credits WHERE critic_run_id = :run_id ORDER BY tp_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchall()

    # train1 has 5 TPs
    assert len(results) == 5, f"Whole-snapshot failure should include all 5 occurrences, got {len(results)}"
    tp_ids = {r.tp_id for r in results}
    assert tp_ids == {"tp-001", "tp-002", "tp-003", "tp-004", "tp-005"}


def test_successful_run_not_affected_by_failure_logic(
    synced_test_session: Session, example_subtract_orm: Example, tp_occurrence_single: tuple[str, str]
):
    """Test that successful critic+grader runs still work correctly."""
    critic_run = make_critic_run(
        example=example_subtract_orm, model="test-critic-model", status=AgentRunStatus.COMPLETED
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

    grader_run = make_grader_run(
        critic_run=critic_run, model="test-grader-model", canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT
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
        text(
            "SELECT grader_run_id, found_credit, grader_rationale FROM occurrence_credits WHERE critic_run_id = :run_id"
        ),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchone()

    assert result is not None
    assert result.grader_run_id is not None
    assert result.found_credit == 0.8
    assert "Partially found" in result.grader_rationale


def test_multiple_occurrences_with_or_logic(synced_test_session: Session, example_multi_tp_orm: Example):
    """Test catchability with OR logic in critic_scopes_expected_to_recall."""
    critic_run = make_critic_run(
        example=example_multi_tp_orm, model="test-critic-model", status=AgentRunStatus.MAX_TURNS_EXCEEDED
    )
    synced_test_session.add(critic_run)
    synced_test_session.commit()

    results = synced_test_session.execute(
        text("SELECT tp_id FROM occurrence_credits WHERE critic_run_id = :run_id ORDER BY tp_id"),
        {"run_id": str(critic_run.agent_run_id)},
    ).fetchall()

    # multi-TP example has 2 occurrences in expected recall scope
    assert len(results) == 2, f"Expected 2 occurrences in recall scope, got {len(results)}"


def test_multiple_grader_runs_do_not_overweight_critic_run(
    synced_test_session: Session, example_subtract_orm: Example, tp_occurrence_single: tuple[str, str]
):
    """Test that multiple grader runs for same critic run don't cause overweighting."""
    # Create 1 failed critic run
    failed_run = make_critic_run(example=example_subtract_orm, status=AgentRunStatus.MAX_TURNS_EXCEEDED)
    synced_test_session.add(failed_run)

    # Create 1 successful critic run with 3 grader runs at different credits
    successful_run = make_critic_run(example=example_subtract_orm, model="test-model", status=AgentRunStatus.COMPLETED)
    synced_test_session.add(successful_run)
    synced_test_session.flush()

    for idx, credit in enumerate([0.5, 0.6, 0.7]):
        make_grader_run_with_credit(
            session=synced_test_session,
            critic_run=successful_run,
            tp_occurrence=tp_occurrence_single,
            credit=credit,
            input_idx=idx,
        )

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByDefinitionSplitKind)
        .filter_by(critic_image_digest=CRITIC_IMAGE_REF, split=Split.TRAIN, critic_model="test-model")
        .one()
    )

    assert sum(result.status_counts.values()) == 2, "Should count both critic runs"
    assert result.status_counts[AgentRunStatus.COMPLETED] == 1
    assert result.status_counts[AgentRunStatus.MAX_TURNS_EXCEEDED] == 1

    # Mean: (0.0 + avg(0.5,0.6,0.7)) / 2 = (0.0 + 0.6) / 2 = 0.3
    avg_caught = result.credit_stats.mean if result.credit_stats else 0.0
    assert abs(avg_caught - 0.3) < 0.01, f"credit_stats.mean should be 0.3, got {avg_caught}"
    assert result.recall_denominator == 1


def test_aggregated_view_counts_total_and_failed_runs(synced_test_session: Session, example_subtract_orm: Example):
    """Test that aggregated_recall_by_definition includes failure counts."""
    # Create 3 successful runs with grader runs
    for _ in range(3):
        critic_run = make_critic_run(example=example_subtract_orm, status=AgentRunStatus.COMPLETED)
        synced_test_session.add(critic_run)
        synced_test_session.flush()
        grader_run = make_grader_run(
            critic_run=critic_run, model="test-grader-model", canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT
        )
        synced_test_session.add(grader_run)

    # Create 2 max_turns_exceeded failures
    for _ in range(2):
        synced_test_session.add(make_critic_run(example=example_subtract_orm, status=AgentRunStatus.MAX_TURNS_EXCEEDED))

    # Create 1 context_length_exceeded failure
    synced_test_session.add(
        make_critic_run(example=example_subtract_orm, status=AgentRunStatus.CONTEXT_LENGTH_EXCEEDED)
    )

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByDefinitionSplitKind)
        .filter_by(critic_image_digest=CRITIC_IMAGE_REF, split=Split.TRAIN, critic_model="test-model")
        .one()
    )

    assert sum(result.status_counts.values()) == 6
    assert result.status_counts[AgentRunStatus.COMPLETED] == 3
    assert result.status_counts[AgentRunStatus.MAX_TURNS_EXCEEDED] == 2
    assert result.status_counts[AgentRunStatus.CONTEXT_LENGTH_EXCEEDED] == 1


def test_aggregated_view_counts_zero_when_no_failures(synced_test_session: Session, example_subtract_orm: Example):
    """Test that failure counts are zero when all runs succeed."""
    for _ in range(3):
        critic_run = make_critic_run(example=example_subtract_orm, status=AgentRunStatus.COMPLETED)
        synced_test_session.add(critic_run)
        synced_test_session.flush()
        grader_run = make_grader_run(
            critic_run=critic_run, model="test-grader-model", canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT
        )
        synced_test_session.add(grader_run)

    synced_test_session.commit()

    result = (
        synced_test_session.query(RecallByDefinitionSplitKind)
        .filter_by(critic_image_digest=CRITIC_IMAGE_REF, split=Split.TRAIN, critic_model="test-model")
        .one()
    )

    assert sum(result.status_counts.values()) == 3
    assert result.status_counts[AgentRunStatus.COMPLETED] == 3
    assert result.status_counts.get(AgentRunStatus.MAX_TURNS_EXCEEDED, 0) == 0


def test_aggregated_recall_by_example_has_correct_weighting(
    synced_test_session: Session, example_subtract_orm: Example, tp_occurrence_single: tuple[str, str]
):
    """Test that aggregated_recall_by_example correctly weights critic runs."""
    # Create 1 failed critic run
    failed_run = make_critic_run(example=example_subtract_orm, status=AgentRunStatus.MAX_TURNS_EXCEEDED)
    synced_test_session.add(failed_run)

    # Create 1 successful critic run with 3 grader runs at different credits
    successful_run = make_critic_run(example=example_subtract_orm, model="test-model", status=AgentRunStatus.COMPLETED)
    synced_test_session.add(successful_run)
    synced_test_session.flush()

    for idx, credit in enumerate([0.4, 0.5, 0.6]):
        make_grader_run_with_credit(
            session=synced_test_session,
            critic_run=successful_run,
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
    assert result.status_counts[AgentRunStatus.COMPLETED] == 1
    assert result.status_counts[AgentRunStatus.MAX_TURNS_EXCEEDED] == 1

    # Mean: (0.0 + avg(0.4,0.5,0.6)) / 2 = (0.0 + 0.5) / 2 = 0.25
    avg_caught = result.credit_stats.mean if result.credit_stats else 0.0
    assert abs(avg_caught - 0.25) < 0.01, f"Expected 0.25, got {avg_caught}"
    assert result.recall_denominator == 1


def test_occurrence_statistics_has_correct_n_critic_runs(
    synced_test_session: Session, subtract_file_example: SingleFileSetExample, tp_occurrence_single: tuple[str, str]
):
    """Test that aggregated_recall_by_example counts critic runs correctly."""
    example = Example.from_spec(synced_test_session, subtract_file_example)

    # Critic run 1: graded 1 time (credit 0.8)
    run1 = make_critic_run(example=example, model="test-model", status=AgentRunStatus.COMPLETED)
    synced_test_session.add(run1)
    synced_test_session.flush()
    make_grader_run_with_credit(
        session=synced_test_session, critic_run=run1, tp_occurrence=tp_occurrence_single, credit=0.8, input_idx=0
    )

    # Critic run 2: graded 4 times (credits 0.5, 0.6, 0.7, 0.8)
    run2 = make_critic_run(example=example, model="test-model", status=AgentRunStatus.COMPLETED)
    synced_test_session.add(run2)
    synced_test_session.flush()
    for idx, credit in enumerate([0.5, 0.6, 0.7, 0.8]):
        make_grader_run_with_credit(
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
    assert result.status_counts[AgentRunStatus.COMPLETED] == 2

    # Run 1: 0.8, Run 2: avg(0.5,0.6,0.7,0.8) = 0.65 -> mean = 0.725
    avg_caught = result.credit_stats.mean if result.credit_stats else 0.0
    assert abs(avg_caught - 0.725) < 0.01, f"Expected 0.725, got {avg_caught}"
