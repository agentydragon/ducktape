"""Pytest configuration for props/core tests.

This conftest.py imports fixtures from the testing package and exposes them
for pytest auto-discovery. Tests anywhere in props/core/ will have access
to these fixtures.
"""

# Register shared fixtures from other packages
pytest_plugins = [
    "agent_core_testing.fixtures",  # Recording handler, make_test_agent, etc.
    "agent_core_testing.responses",  # make_step_runner, responses_factory, etc.
    "mcp_infra.testing.fixtures",  # async_docker_client, make_compositor, etc.
]

# Import fixtures from our testing package for pytest discovery
# Re-export factory functions (not fixtures, but commonly used in tests)
from props.testing.fixtures import (  # noqa: E402, F401  # noqa: E402, F401
    EMPTY_CANONICAL_ISSUES_SNAPSHOT,
    # db
    TEST_FIXTURES_PATH,
    admin_engine,
    # scopes
    all_files_scope,
    block_production_config_in_tests,
    # ground_truth
    example_multi_tp_orm,
    example_subtract_orm,
    fp_id,
    fp_occurrence,
    fp_occurrence_id,
    get_tp_occurrences_for_snapshot,
    make_critic_and_grader_run,
    make_critic_run,
    make_fp_occurrence,
    make_grader_run,
    make_grader_run_with_credit,
    # e2e
    make_openai_client,
    make_reported_issues,
    make_tp_occurrence,
    mock_snapshot_slug,
    noop_openai_client,
    pytest_addoption,
    # runs
    rationale_model,
    run_critic_with_steps,
    run_improvement_agent_with_steps,
    run_prompt_optimizer_with_steps,
    session_monkeypatch,
    subtract_file_example,
    success_termination,
    synced_readonly_session,
    synced_test_db,
    synced_test_session,
    test_db,
    test_registry,
    test_snapshot,
    test_specimens_base,
    test_train_example_with_runs,
    test_trivial_snapshot,
    test_valid_example_with_runs,
    test_validation_snapshot,
    test_validation_snapshot_slug,
    test_workspace_manager,
    tp_occurrence_single,
    tp_occurrences_multi,
    tp_single_id,
    tp_single_occurrence_id,
)

# Re-export mocks for direct imports
from props.testing.mocks import PropsMock  # noqa: E402, F401
