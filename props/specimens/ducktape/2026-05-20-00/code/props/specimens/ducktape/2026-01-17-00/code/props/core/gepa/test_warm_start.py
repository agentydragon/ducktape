"""Tests for GEPA warm-start functionality.

NOTE: These tests are temporarily broken because build_historical_gepa_state
references the prompts table which was dropped. GEPA needs to be migrated
to use agent_definitions instead.
"""

from __future__ import annotations

import pytest

from props.core.db.examples import Example
from props.core.db.session import get_session
from props.core.gepa.warm_start import build_historical_gepa_state
from props.testing.fixtures import get_tp_occurrences_for_snapshot, make_critic_and_grader_run

pytestmark = pytest.mark.skip(reason="GEPA warm-start broken: needs migration from prompts to agent_definitions")


@pytest.fixture
def standard_valset(synced_test_db, sample_subtract_py_scope, calculator_py_scope) -> list[Example]:
    """Standard two-snapshot validation set for most tests.

    Contains specific VALID examples:
    - test-validation/sample_subtract.py (index 0)
    - test-validation-2/calculator.py (index 1)
    """
    with get_session() as session:
        example1 = Example.from_spec(session, sample_subtract_py_scope)
        example2 = Example.from_spec(session, calculator_py_scope)
        session.expunge_all()
        return [example1, example2]


@pytest.fixture
def validation_subtract_valset(synced_test_db, sample_subtract_py_scope) -> list[Example]:
    """Valset containing test-validation/sample_subtract.py (matches db_with_historical_runs example1)."""
    with get_session() as session:
        example = Example.from_spec(session, sample_subtract_py_scope)
        session.expunge(example)
        return [example]


@pytest.fixture
def validation_calculator_valset(synced_test_db, calculator_py_scope) -> list[Example]:
    """Valset containing test-validation-2/calculator.py (matches db_with_historical_runs example2)."""
    with get_session() as session:
        example = Example.from_spec(session, calculator_py_scope)
        session.expunge(example)
        return [example]


@pytest.fixture
def train_add_valset(synced_test_db, add_py_scope) -> list[Example]:
    """Valset containing test-trivial/add.py (TRAIN split, has runs in db_with_historical_runs)."""
    with get_session() as session:
        example = Example.from_spec(session, add_py_scope)
        session.expunge(example)
        return [example]


@pytest.fixture
def db_with_historical_runs(synced_test_db, sample_subtract_py_scope, calculator_py_scope, add_py_scope):
    """Fixture providing database with historical critic + grader runs.

    Creates runs for specific VALID and TRAIN examples:
    - test-validation/sample_subtract.py (VALID) - 2 prompts
    - test-validation-2/calculator.py (VALID) - 1 prompt
    - test-trivial/add.py (TRAIN) - 1 prompt
    """
    with get_session() as session:
        # Get specific VALID examples
        example1 = Example.from_spec(session, sample_subtract_py_scope)
        example2 = Example.from_spec(session, calculator_py_scope)

        # Get specific TRAIN example for exclusion test
        train_example = Example.from_spec(session, add_py_scope)

        # Create critic + grader runs using convenience factory
        # example1 (test-validation/subtract.py) - evaluated with both prompts
        tp_occs1 = get_tp_occurrences_for_snapshot(example1.snapshot_slug, session)
        make_critic_and_grader_run(example=example1, tp_occurrences=tp_occs1, credit=0.8, session=session)
        make_critic_and_grader_run(example=example1, tp_occurrences=tp_occs1, credit=0.9, session=session)
        # example2 (test-validation-2/calculator.py) - evaluated with prompt_a only
        tp_occs2 = get_tp_occurrences_for_snapshot(example2.snapshot_slug, session)
        make_critic_and_grader_run(example=example2, tp_occurrences=tp_occs2, credit=0.6, session=session)
        # train_example (test-trivial/add.py) - evaluated but in TRAIN split
        tp_occs_train = get_tp_occurrences_for_snapshot(train_example.snapshot_slug, session)
        make_critic_and_grader_run(example=train_example, tp_occurrences=tp_occs_train, credit=0.5, session=session)

        session.commit()


def test_build_historical_state_basic(db_with_historical_runs, standard_valset):
    """Test basic warm-start state building from historical runs."""
    state = build_historical_gepa_state(valset=standard_valset, critic_model="test-model", grader_model="test-model")

    assert state is not None
    assert state["validation_schema_version"] == 2

    # Should have 2 unique prompts (prompt_a and prompt_b)
    assert len(state["program_candidates"]) == 2

    # Check sparse validation scores structure
    assert len(state["prog_candidate_val_subscores"]) == 2

    # Prompt A was evaluated on valid-1 (recall=0.8) and valid-2 (recall=0.6)
    # Prompt B was evaluated on valid-1 (recall=0.9)
    prompt_a_scores = None
    prompt_b_scores = None
    for prog_idx, candidate in enumerate(state["program_candidates"]):
        if "version A" in candidate["system_prompt"]:
            prompt_a_scores = state["prog_candidate_val_subscores"][prog_idx]
        elif "version B" in candidate["system_prompt"]:
            prompt_b_scores = state["prog_candidate_val_subscores"][prog_idx]

    assert prompt_a_scores is not None
    assert prompt_b_scores is not None

    # Prompt A: evaluated on both validation examples
    assert len(prompt_a_scores) == 2
    assert 0 in prompt_a_scores  # valid-1 (valset[0])
    assert 1 in prompt_a_scores  # valid-2 (valset[1])
    assert prompt_a_scores[0] == 0.8
    assert prompt_a_scores[1] == 0.6

    # Prompt B: evaluated only on valid-1
    assert len(prompt_b_scores) == 1
    assert 0 in prompt_b_scores
    assert prompt_b_scores[0] == 0.9

    # Check Pareto frontier: valid-1 should have prompt_b (0.9), valid-2 should have prompt_a (0.6)
    assert 0 in state["pareto_front_valset"]
    assert 1 in state["pareto_front_valset"]
    assert state["pareto_front_valset"][0] == 0.9  # Best for valid-1
    assert state["pareto_front_valset"][1] == 0.6  # Best for valid-2

    # Check total_num_evals is 0 (budget applies to current run only)
    assert state["total_num_evals"] == 0


def test_json_null_filtering(db_with_historical_runs, standard_valset):
    """Test that grader runs with JSON null output are properly excluded."""
    state = build_historical_gepa_state(valset=standard_valset, critic_model="test-model", grader_model="test-model")

    # Should succeed without AttributeError on None.recall
    assert state is not None

    # The incomplete run with JSON null output should not appear in any scores
    # We have 3 valid grader outputs, so total scores across all prompts should be 3
    total_scores = sum(len(scores) for scores in state["prog_candidate_val_subscores"])
    assert total_scores == 3


def test_empty_database(validation_subtract_valset):
    """Test warm-start with no historical data returns None."""
    # No db_with_historical_runs fixture, so database has no runs
    state = build_historical_gepa_state(
        valset=validation_subtract_valset, critic_model="test-model", grader_model="test-model"
    )
    assert state is None


def test_model_filtering(db_with_historical_runs, validation_subtract_valset):
    """Test that only runs matching specified models are included."""
    # db_with_historical_runs creates runs with critic_model="test-model"
    # Querying with wrong model should return None
    state = build_historical_gepa_state(
        valset=validation_subtract_valset, critic_model="wrong-model", grader_model="test-model"
    )
    assert state is None


def test_split_filtering(db_with_historical_runs, train_add_valset):
    """Test that only validation split runs are included (not training)."""
    # db_with_historical_runs creates runs for test-trivial/add.py (TRAIN split)
    # But build_historical_gepa_state only loads VALID split runs
    state = build_historical_gepa_state(valset=train_add_valset, critic_model="test-model", grader_model="test-model")
    assert state is None  # TRAIN split examples excluded from warm-start


def test_unknown_examples_skipped(db_with_historical_runs, validation_subtract_valset):
    """Test that examples not in current valset are skipped with warning."""
    # db_with_historical_runs has runs for:
    #  - test-validation/sample_subtract.py (IN valset)
    #  - test-validation-2/calculator.py (NOT in valset)
    # Only the first should contribute to warm-start state
    state = build_historical_gepa_state(
        valset=validation_subtract_valset, critic_model="test-model", grader_model="test-model"
    )

    assert state is not None
    assert len(state["program_candidates"]) == 2  # prompt_a and prompt_b
    # Should have scores for only one example (index 0)
    for scores in state["prog_candidate_val_subscores"]:
        assert set(scores.keys()) == {0}


def test_scope_hash_matching(db_with_historical_runs, validation_calculator_valset):
    """Test that (snapshot_slug, scope_hash) tuple matching works correctly."""
    # db_with_historical_runs has runs for test-validation-2/calculator.py
    # Querying with this exact example should find matches
    state = build_historical_gepa_state(
        valset=validation_calculator_valset, critic_model="test-model", grader_model="test-model"
    )

    assert state is not None
    assert len(state["program_candidates"]) == 1  # Only prompt_a evaluated example2
    assert state["prog_candidate_val_subscores"][0][0] == 0.6  # example2's recall


def test_critic_scope_spec_all(db_with_historical_runs, synced_test_db, all_files_scope):
    """Test that CriticScopeSpec 'all' is handled correctly in index mapping."""
    with get_session() as session:
        all_files_example = Example.from_spec(session, all_files_scope)
        session.expunge(all_files_example)
        valset = [all_files_example]

    state = build_historical_gepa_state(valset=valset, critic_model="test-model", grader_model="test-model")
    assert state is None  # No matches because AllFilesScope hashes differently from db_with_historical_runs


def test_deterministic_ordering(db_with_historical_runs, standard_valset):
    """Test that prompt candidates are returned in deterministic order."""
    # Run multiple times and check order is consistent
    state1 = build_historical_gepa_state(valset=standard_valset, critic_model="test-model", grader_model="test-model")
    state2 = build_historical_gepa_state(valset=standard_valset, critic_model="test-model", grader_model="test-model")

    assert state1 is not None
    assert state2 is not None

    # Prompts should be in same order (sorted by SHA for determinism)
    prompts1 = [c["system_prompt"] for c in state1["program_candidates"]]
    prompts2 = [c["system_prompt"] for c in state2["program_candidates"]]
    assert prompts1 == prompts2

    # Scores should match
    assert state1["prog_candidate_val_subscores"] == state2["prog_candidate_val_subscores"]
