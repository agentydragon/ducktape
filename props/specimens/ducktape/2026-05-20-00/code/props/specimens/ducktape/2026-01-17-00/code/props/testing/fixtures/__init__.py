"""Test fixtures for props tests.

Re-exports all fixtures from submodules for convenient importing.
"""

# Database fixtures
from props.testing.fixtures.db import (
    TEST_FIXTURES_PATH,
    admin_engine,
    block_production_config_in_tests,
    pytest_addoption,
    session_monkeypatch,
    synced_readonly_session,
    synced_test_db,
    synced_test_session,
    test_db,
    test_specimens_base,
)

# E2E fixtures
from props.testing.fixtures.e2e import (
    make_openai_client,
    mock_snapshot_slug,
    noop_openai_client,
    run_critic_with_steps,
    run_improvement_agent_with_steps,
    run_prompt_optimizer_with_steps,
    success_termination,
    test_registry,
    test_workspace_manager,
)

# Ground truth fixtures
from props.testing.fixtures.ground_truth import (
    example_multi_tp_orm,
    example_subtract_orm,
    fp_id,
    fp_occurrence,
    fp_occurrence_id,
    get_tp_occurrences_for_snapshot,
    make_fp_occurrence,
    make_tp_occurrence,
    tp_occurrence_single,
    tp_occurrences_multi,
    tp_single_id,
    tp_single_occurrence_id,
)

# Run fixtures
from props.testing.fixtures.runs import (
    EMPTY_CANONICAL_ISSUES_SNAPSHOT,
    make_critic_and_grader_run,
    make_critic_run,
    make_grader_run,
    make_grader_run_with_credit,
    make_reported_issues,
    rationale_model,
    test_snapshot,
    test_train_example_with_runs,
    test_trivial_snapshot,
    test_valid_example_with_runs,
    test_validation_snapshot,
    test_validation_snapshot_slug,
)

# Scope fixtures
from props.testing.fixtures.scopes import all_files_scope, subtract_file_example

__all__ = [
    # runs
    "EMPTY_CANONICAL_ISSUES_SNAPSHOT",
    # db
    "TEST_FIXTURES_PATH",
    "admin_engine",
    # scopes
    "all_files_scope",
    "block_production_config_in_tests",
    # ground_truth
    "example_multi_tp_orm",
    "example_subtract_orm",
    "fp_id",
    "fp_occurrence",
    "fp_occurrence_id",
    "get_tp_occurrences_for_snapshot",
    "make_critic_and_grader_run",
    "make_critic_run",
    "make_fp_occurrence",
    "make_grader_run",
    "make_grader_run_with_credit",
    # e2e
    "make_openai_client",
    "make_reported_issues",
    "make_tp_occurrence",
    "mock_snapshot_slug",
    "noop_openai_client",
    "pytest_addoption",
    "rationale_model",
    "run_critic_with_steps",
    "run_improvement_agent_with_steps",
    "run_prompt_optimizer_with_steps",
    "session_monkeypatch",
    "subtract_file_example",
    "success_termination",
    "synced_readonly_session",
    "synced_test_db",
    "synced_test_session",
    "test_db",
    "test_registry",
    "test_snapshot",
    "test_specimens_base",
    "test_train_example_with_runs",
    "test_trivial_snapshot",
    "test_valid_example_with_runs",
    "test_validation_snapshot",
    "test_validation_snapshot_slug",
    "test_workspace_manager",
    "tp_occurrence_single",
    "tp_occurrences_multi",
    "tp_single_id",
    "tp_single_occurrence_id",
]
