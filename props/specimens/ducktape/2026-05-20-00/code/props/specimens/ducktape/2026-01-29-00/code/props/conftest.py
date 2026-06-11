"""Pytest configuration for props tests.

This conftest.py imports fixtures from the testing package and exposes them
for pytest auto-discovery. Tests anywhere in props/ will have access
to these fixtures.
"""

import pytest

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core_testing.fixtures import *  # noqa: F403
from agent_core_testing.responses import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403

# Import fixtures from our testing package for pytest discovery
# Re-export factory functions (not fixtures, but commonly used in tests)
# Testcontainers fixtures - imported directly from defining module
from props.testing.fixtures.db import (  # noqa: F401  # noqa: F401
    TEST_FIXTURES_PATH,
    admin_engine,
    postgres_base_config,
    postgres_container,
    pytest_addoption,
    session_monkeypatch,
    synced_readonly_session,
    synced_test_db,
    synced_test_session,
    test_db,
    test_specimens_base,
)
from props.testing.fixtures.e2e import (  # noqa: F401
    make_openai_client,
    mock_snapshot_slug,
    noop_openai_client,
    success_termination,
)

# Import e2e container fixture directly from its module
from props.testing.fixtures.e2e_container import e2e_stack  # noqa: F401
from props.testing.fixtures.ground_truth import (  # noqa: F401
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
from props.testing.fixtures.runs import (  # noqa: F401
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
from props.testing.fixtures.scopes import all_files_scope, subtract_file_example  # noqa: F401

# Re-export mocks for direct imports
from props.testing.mocks import PropsMock  # noqa: F401


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"
